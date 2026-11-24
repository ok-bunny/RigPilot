using System.Globalization;
using System.Text;
using LibreHardwareMonitor.Hardware;

namespace RigPilot.CpuTemperature;

internal static class Program
{
    // Windows monitor backends commonly expose 0 °C when the low-level CPU
    // sensor cannot be opened without administrator/SYSTEM rights.  Treat it
    // as unavailable instead of publishing a plausible-looking false value.
    private const double MinimumTemperatureC = 1.0;
    private const double MaximumTemperatureC = 150.0;

    public static int Main(string[] args)
    {
        if (args.Length == 1 && string.Equals(args[0], "--version", StringComparison.Ordinal))
        {
            Console.WriteLine("RigPilot CPU Temperature Helper 1.0.0 (LibreHardwareMonitor 0.9.6)");
            return 0;
        }
        if (args.Length != 1 || !string.Equals(args[0], "--json", StringComparison.Ordinal))
        {
            Console.Error.WriteLine("usage: RigPilot.CpuTemperature.exe --json");
            return 64;
        }

        Computer? computer = null;
        try
        {
            // The helper is intentionally CPU-only.  It never enables GPU,
            // storage, memory, motherboard, controller, or network sensors.
            computer = new Computer { IsCpuEnabled = true };
            computer.Open();

            var candidates = new List<Candidate>();
            foreach (IHardware hardware in computer.Hardware)
            {
                CollectCpuTemperatureCandidates(hardware, candidates);
            }

            Candidate? selected = candidates
                .OrderByDescending(candidate => candidate.Score)
                .ThenByDescending(candidate => candidate.TemperatureC)
                .FirstOrDefault();
            if (selected is null)
            {
                return 2;
            }

            Console.WriteLine(ToJson(
                Math.Round(selected.TemperatureC, 1),
                selected.SensorName,
                "LibreHardwareMonitor 0.9.6"));
            return 0;
        }
        catch (Exception exception)
        {
            // Keep stderr short and deterministic.  The Agent treats this as
            // optional telemetry and never forwards exception details.
            Console.Error.WriteLine("CPU temperature unavailable: " + exception.GetType().Name);
            return 3;
        }
        finally
        {
            if (computer is not null)
            {
                try
                {
                    computer.Close();
                }
                catch
                {
                    // Closing a best-effort read-only sensor must not mask the
                    // original result or affect the Agent process.
                }
            }
        }
    }

    private static void CollectCpuTemperatureCandidates(
        IHardware hardware,
        ICollection<Candidate> candidates)
    {
        if (hardware.HardwareType != HardwareType.Cpu)
        {
            return;
        }

        hardware.Update();
        foreach (ISensor sensor in hardware.Sensors)
        {
            if (sensor.SensorType != SensorType.Temperature || !sensor.Value.HasValue)
            {
                continue;
            }

            double temperature = sensor.Value.Value;
            if (double.IsNaN(temperature)
                || double.IsInfinity(temperature)
                || temperature < MinimumTemperatureC
                || temperature > MaximumTemperatureC)
            {
                continue;
            }

            int score = SensorScore(sensor.Name);
            if (score > 0)
            {
                candidates.Add(new Candidate(temperature, SafeSensorName(sensor.Name), score));
            }
        }

        foreach (IHardware child in hardware.SubHardware)
        {
            CollectCpuTemperatureCandidates(child, candidates);
        }
    }

    private static int SensorScore(string? sensorName)
    {
        string name = (sensorName ?? string.Empty).Trim().ToLowerInvariant();
        if (name.Length == 0)
        {
            return 0;
        }

        if (ContainsOrdinal(name, "tctl") && ContainsOrdinal(name, "tdie"))
        {
            return 150;
        }
        if (name is "cpu package" or "package" || name.StartsWith("package id ", StringComparison.Ordinal))
        {
            return 145;
        }
        if (name is "core (tdie)" or "cpu (tdie)")
        {
            return 140;
        }
        if (ContainsOrdinal(name, "tdie"))
        {
            // CCD/individual-die temperatures are useful only when no whole
            // package/die sensor is available.
            return name.StartsWith("ccd", StringComparison.Ordinal) ? 85 : 130;
        }
        if (ContainsOrdinal(name, "tctl"))
        {
            return 135;
        }
        if (name is "core max" or "cpu" or "cpu temperature" or "cpu temp")
        {
            return 100;
        }

        // A positively identified CPU hardware object can still expose only
        // individual cores on some Intel systems.  Keep that as a last-resort
        // reading below package/die sensors.
        return name.StartsWith("cpu core", StringComparison.Ordinal)
            || name.StartsWith("core #", StringComparison.Ordinal)
            || name.StartsWith("core ", StringComparison.Ordinal)
            ? 60
            : 0;
    }

    private static string SafeSensorName(string? value)
    {
        string compact = string.Join(
            " ",
            (value ?? "CPU Package").Split(
                (char[]?)null,
                StringSplitOptions.RemoveEmptyEntries));
        if (compact.Length == 0)
        {
            return "CPU Package";
        }
        return compact.Length <= 80 ? compact : compact.Substring(0, 80);
    }

    private static bool ContainsOrdinal(string value, string part)
    {
        return value.IndexOf(part, StringComparison.Ordinal) >= 0;
    }

    private static string ToJson(double temperatureC, string sensorName, string source)
    {
        return "{\"temperature_c\":"
            + temperatureC.ToString("0.0", CultureInfo.InvariantCulture)
            + ",\"sensor_name\":\""
            + EscapeJson(sensorName)
            + "\",\"source\":\""
            + EscapeJson(source)
            + "\"}";
    }

    private static string EscapeJson(string value)
    {
        var result = new StringBuilder(value.Length + 8);
        foreach (char character in value)
        {
            switch (character)
            {
                case '\"':
                    result.Append("\\\"");
                    break;
                case '\\':
                    result.Append("\\\\");
                    break;
                case '\b':
                    result.Append("\\b");
                    break;
                case '\f':
                    result.Append("\\f");
                    break;
                case '\n':
                    result.Append("\\n");
                    break;
                case '\r':
                    result.Append("\\r");
                    break;
                case '\t':
                    result.Append("\\t");
                    break;
                default:
                    if (character < 0x20)
                    {
                        result.Append("\\u");
                        result.Append(((int)character).ToString("x4", CultureInfo.InvariantCulture));
                    }
                    else
                    {
                        result.Append(character);
                    }
                    break;
            }
        }
        return result.ToString();
    }

    private sealed class Candidate
    {
        public Candidate(double temperatureC, string sensorName, int score)
        {
            TemperatureC = temperatureC;
            SensorName = sensorName;
            Score = score;
        }

        public double TemperatureC { get; }
        public string SensorName { get; }
        public int Score { get; }
    }
}

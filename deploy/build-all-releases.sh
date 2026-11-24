#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "${SCRIPT_ROOT}/.." && pwd -P)"
OUTPUT_DIR="${PROJECT_ROOT}/outputs"
SKIP_TESTS=0
SKIP_LINUX=0
SKIP_CONTROLLER=0
VERSION_PATTERN='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'

while (($#)); do
    case "$1" in
        --output-dir)
            (($# >= 2)) || { echo "--output-dir requires a path" >&2; exit 2; }
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --skip-tests) SKIP_TESTS=1; shift ;;
        --skip-linux) SKIP_LINUX=1; shift ;;
        --skip-controller) SKIP_CONTROLLER=1; shift ;;
        # Accepted for callers that already selected Linux-only builds.
        --skip-windows) shift ;;
        -h|--help)
            echo "Build local Linux Agent and Controller releases; Windows builds run separately."
            echo "Usage: bash deploy/build-all-releases.sh [--output-dir DIR] [--skip-tests] [--skip-linux] [--skip-controller]"
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

[[ "$(uname -s)" == "Linux" ]] || {
    echo "Run this entry point in the Ubuntu build environment." >&2
    exit 2
}
if [[ "${SKIP_LINUX}" -eq 1 && "${SKIP_CONTROLLER}" -eq 1 ]]; then
    echo "No local release target selected." >&2
    exit 2
fi
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
OUTPUT_DIR="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "${OUTPUT_DIR}")"
cd -- "${PROJECT_ROOT}"
AGENT_VERSION=""
CONTROLLER_VERSION=""
if [[ "${SKIP_LINUX}" -eq 0 ]]; then
    AGENT_VERSION="$(python3 -c 'from agent.version import __version__; print(__version__)')"
    [[ "${AGENT_VERSION}" =~ ${VERSION_PATTERN} ]] || {
        echo "Could not read a valid Agent source version." >&2
        exit 1
    }
fi
if [[ "${SKIP_CONTROLLER}" -eq 0 ]]; then
    CONTROLLER_VERSION="$(python3 -c 'from server import __version__; print(__version__)')"
    [[ "${CONTROLLER_VERSION}" =~ ${VERSION_PATTERN} ]] || {
        echo "Could not read a valid Controller source version." >&2
        exit 1
    }
fi
mkdir -p -- "${OUTPUT_DIR}"
declare -a PLATFORMS=()
declare -a VERIFY_ARGS=(
    "${SCRIPT_ROOT}/verify-release-artifacts.py"
    --output-dir "${OUTPUT_DIR}"
    --skip-windows
)
if [[ "${SKIP_LINUX}" -eq 0 ]]; then
    RIGPILOT_SKIP_TESTS="${SKIP_TESTS}" \
        bash "${SCRIPT_ROOT}/linux/build-ubuntu-portable.sh" "${AGENT_VERSION}" "${OUTPUT_DIR}"
    PLATFORMS+=(linux)
    VERIFY_ARGS+=(--agent-version "${AGENT_VERSION}")
fi
if [[ "${SKIP_CONTROLLER}" -eq 0 ]]; then
    python3 "${SCRIPT_ROOT}/build-controller-release.py" \
        --version "${CONTROLLER_VERSION}" --output-dir "${OUTPUT_DIR}"
    PLATFORMS+=(controller)
    VERIFY_ARGS+=(--controller-version "${CONTROLLER_VERSION}")
fi

[[ "${SKIP_LINUX}" -eq 1 ]] && VERIFY_ARGS+=(--skip-linux)
[[ "${SKIP_CONTROLLER}" -eq 1 ]] && VERIFY_ARGS+=(--skip-controller)
[[ "${SKIP_LINUX}" -eq 0 ]] && VERIFY_ARGS+=(--smoke-native linux)
python3 "${VERIFY_ARGS[@]}"

TARGETS="$(IFS=,; echo "${PLATFORMS[*]}")"
printf 'BUILD_ONLY=1\nBUILD_PLATFORMS=%s\nOUTPUT_DIR=%s\nAGENT_VERSION=%s\nCONTROLLER_VERSION=%s\n' \
    "${TARGETS}" "${OUTPUT_DIR}" "${AGENT_VERSION}" "${CONTROLLER_VERSION}"

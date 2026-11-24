"""Explicit synthetic archive download callbacks for miner API fixtures."""
import hashlib
from pathlib import Path

from controller_test_support import auth


def package_download(content: bytes):
    """Return a fresh callback preserving the supplied archive bytes and digest."""
    def download(url: str, destination: Path, **kwargs):
        del url, kwargs
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return len(content), hashlib.sha256(content).hexdigest()

    return download


def create_wallet(client, token: str, coin: str) -> str:
    """Create the public synthetic wallet used by native GPU miner tests."""
    response = client.post(
        "/api/wallets",
        headers=auth(token),
        json={
            "name": f"{coin} wallet",
            "coin": coin,
            "address": f"public-{coin.lower()}-wallet-address",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]

"""Container probe for process, provisioned database and recent work polling."""

from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


def main() -> None:
    client = build_opener(ProxyHandler({}))
    try:
        for name in ("live", "ready", "worker"):
            with client.open(
                f"http://127.0.0.1:8080/health/{name}", timeout=1
            ) as response:
                if response.status != 200:
                    raise SystemExit(1)
    except (URLError, TimeoutError):
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

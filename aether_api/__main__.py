"""Start the API:  python -m aether_api   (always on 127.0.0.1; there is deliberately no --host option)."""

import argparse
import json
import sys

from aether_api import BIND_HOST, DEFAULT_PORT, DEV_PORT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aether_api", description="AETHER read-only web API.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--dev", action="store_true",
                        help=f"also accept the Vite development server at localhost:{DEV_PORT}")
    parser.add_argument("--dump-openapi", action="store_true",
                        help="print the OpenAPI document (for generating TypeScript types) and exit")
    args = parser.parse_args(argv)

    from aether_api.app import create_app
    from aether_api.security import SecuritySettings

    app = create_app(SecuritySettings(port=args.port, dev=args.dev))
    if args.dump_openapi:
        json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    import uvicorn

    print(f"AETHER API on http://{BIND_HOST}:{args.port}/api/v1/health (read-only; Ctrl+C to stop)")
    uvicorn.run(app, host=BIND_HOST, port=args.port, server_header=False, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

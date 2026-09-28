"""`python -m freeagent_bind` で起動するためのエントリポイント（stdio の JSON-RPC 専用）。"""

from .server import main

if __name__ == "__main__":
    main()

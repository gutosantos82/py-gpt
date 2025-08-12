import sys
from pathlib import Path

# Ensure 'src' directory is on sys.path for imports (src layout)
ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import logging
logging.basicConfig( level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s" )


from pygpt_net.app import run
from pygpt_net.plugin.telegram_gateway import Plugin as TelegramGatewayPlugin

if __name__ == "__main__":
    plugins = [TelegramGatewayPlugin()]
    run(plugins=plugins)

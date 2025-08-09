from pygpt_net.app import run
from plugins.telegram_gateway import Plugin as TelegramGatewayPlugin

if __name__ == "__main__":
    plugins = [TelegramGatewayPlugin()]
    run(plugins=plugins)

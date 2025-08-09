"""
Telegram Gateway plugin for PyGPT.

What it does
------------
- Starts a Telegram bot in the background when the plugin is enabled.
- Reads the bot token from PyGPT's plugin options (editable in UI).
- For each incoming text message, calls into PyGPT to get a response.
- Sends the response back to the Telegram chat.

Requirements
------------
pip install python-telegram-bot>=20

Notes
-----
- This file is a standard PyGPT plugin: it exposes a class with:
    - id, name, version, description metadata
    - handle(self, event, *args, **kwargs) -> react to PyGPT events
- UI options are declared via BasePlugin.add_option and appear under
  “Telegram Gateway” in Plugins → Settings.

Fill the “ADAPTER” spot below to call PyGPT's chat pipeline. The two
options shown match the docs pattern; pick whichever matches your build.
"""

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

from pygpt_net.core.events import Event  # event enum (docs list the names)
from pygpt_net.plugin.base.plugin import BasePlugin

# Telegram (python-telegram-bot v20+)
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


@dataclass
class _BotState:
    token: Optional[str] = None
    app_task: Optional[asyncio.Task] = None
    loop: Optional[asyncio.AbstractEventLoop] = None
    thread: Optional[threading.Thread] = None
    tg_app: Optional["telegram.ext.Application"] = None  # type: ignore
    stop_event: threading.Event = field(default_factory=threading.Event)


class Plugin(BasePlugin):
    """Telegram gateway plugin"""

    id = "telegram_gateway"
    name = "Telegram Gateway"
    version = "1.0.0"
    description = "Receive text from Telegram and reply using PyGPT."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = _BotState()  # runtime TG state
        self.allowed_users = set()  # optional allowlist by Telegram user id
        # plugin options
        self.add_option(
            "bot_token",
            type="text",
            label="Telegram Bot Token",
            description="Create a bot with @BotFather and paste the token here.",
            secret=True,
        )
        self.add_option(
            "allowed_user_ids",
            type="text",
            label="Allowed Telegram User IDs (comma-separated, optional)",
            description="Leave blank to allow anyone who knows the bot username.",
        )

    # ---------- PyGPT lifecycle ----------

    def handle(self, event: Event, *args, **kwargs):
        """
        React to PyGPT-dispatched events. See the official event list.
        """
        name = event.name
        data = event.data or {}

        if name == Event.ENABLE:
            # Plugin toggled ON in the UI
            if data.get("value") == self.id:
                log.info("[TelegramGateway] ENABLE received; starting bot")
                self._start_bot()

        elif name == Event.DISABLE:
            if data.get("value") == self.id:
                log.info("[TelegramGateway] DISABLE received; stopping bot")
                self._stop_bot()

        elif name == Event.PLUGIN_SETTINGS_CHANGED:
            # User clicked "Save" in Plugins → Settings
            log.info("[TelegramGateway] Settings changed; restarting bot")
            self._restart_bot()

        # (Optional) If you want to expose a /syntax or inline commands,
        # you can also handle CMD_SYNTAX / CMD_SYNTAX_INLINE here and append help.

    # ---------- Telegram lifecycle ----------

    def _start_bot(self):
        self._stop_bot()  # idempotent
        token = (self.get_option_value("bot_token") or "").strip()
        allow = (self.get_option_value("allowed_user_ids") or "").strip()

        self.allowed_users = {
            int(x) for x in allow.split(",") if x.strip().isdigit()
        } if allow else set()

        if not token:
            log.warning("[TelegramGateway] Bot token is empty; not starting.")
            return

        self.state.stop_event.clear()

        def _runner():
            self.state.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.state.loop)
            self.state.loop.run_until_complete(self._tg_main(token))

        self.state.thread = threading.Thread(target=_runner, name="tg-gateway", daemon=True)
        self.state.thread.start()
        log.info("[TelegramGateway] Telegram bot started")

    def _stop_bot(self):
        if self.state.loop and self.state.tg_app:
            log.info("[TelegramGateway] Stopping Telegram bot...")
            try:
                async def _shutdown():
                    await self.state.tg_app.stop()
                    await self.state.tg_app.shutdown()

                asyncio.run_coroutine_threadsafe(_shutdown(), self.state.loop).result(timeout=10)
            except Exception as e:
                log.debug("shutdown exception: %s", e)

        self.state.stop_event.set()
        self.state.tg_app = None
        self.state.loop = None
        self.state.thread = None
        log.info("[TelegramGateway] Telegram bot stopped")

    def _restart_bot(self):
        self._stop_bot()
        self._start_bot()

    async def _tg_main(self, token: str):
        app = ApplicationBuilder().token(token).build()

        # Message handler: plain text only
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))

        # Optionally, you can add a /start or /help command handler
        # from telegram.ext import CommandHandler
        # app.add_handler(CommandHandler("start", self._on_start))

        await app.initialize()
        self.state.tg_app = app
        await app.start()
        # Idle loop (non-blocking because we run in a thread)
        try:
            await app.updater.start_polling()
            while not self.state.stop_event.is_set():
                await asyncio.sleep(0.25)
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

    # ---------- Telegram handlers ----------

    async def _on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or not update.effective_user:
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        text = (update.message.text or "").strip()

        if self.allowed_users and user_id not in self.allowed_users:
            await context.bot.send_message(
                chat_id=chat_id,
                text="This bot is locked. Your Telegram user ID is not allowed.",
            )
            return

        if not text:
            return

        # Inform user we’re working
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        try:
            reply_text = await self._ask_pygpt(text)
        except Exception as e:
            log.exception("PyGPT error")
            reply_text = f"⚠️ Error while asking PyGPT: {e}"

        # Send back
        await context.bot.send_message(
            chat_id=chat_id,
            text=reply_text or "(no response)",
            parse_mode=ParseMode.MARKDOWN,  # or omit if you prefer plain text
            disable_web_page_preview=True,
        )

    # ---------- Bridge into PyGPT ----------

    async def _ask_pygpt(self, user_text: str) -> str:
        """
        Core bridge: send `user_text` to PyGPT and return the assistant's reply.

        Choose ONE of the adapter blocks below that matches your PyGPT version.
        See 'Extending PyGPT' docs and the 'examples/example_plugin.py' in the repo
        for the exact controller names in your build.
        """
        # ========== ADAPTER A: Event-based injection ==========
        # If your build exposes a public "ask" via a controller, use Adapter B.
        # Otherwise, you can synthesize the same flow the UI does by:
        #   1) Tell PyGPT “the user is sending text” (USER_SEND),
        #   2) Then call the app's "send" method if available (often present).
        try:
            data = {"value": user_text}
            if hasattr(self.window, "events"):
                self.window.events.dispatch(Event.USER_SEND, data=data, ctx=None)
            # Some builds provide a high-level "send" or "ask" on a controller:
            # e.g., self.window.controller.send()  OR  self.window.chat.ask()
            if hasattr(self.window, "controller") and hasattr(self.window.controller, "send"):
                # Synchronous call that returns final assistant text:
                result = self.window.controller.send(user_text)
                if isinstance(result, str):
                    return result
                # If result is a complex object, adapt as needed:
                return getattr(result, "text", "") or str(result)
        except Exception:
            pass

        # ========== ADAPTER B: Controller direct call ==========
        # Many releases expose a chat/ask entry point on a controller or service.
        # Search in your codebase for "def send(" or "def ask(" in controllers.
        # Example patterns seen in plugins/examples:
        #   self.window.chat.ask(user_text)
        #   self.window.controller.chat.ask(user_text)
        #   self.window.core.chat.ask(user_text)
        for path in [
            ("chat", "ask"),
            ("controller", "ask"),
            ("controller", "chat", "ask"),
            ("core", "chat", "ask"),
        ]:
            try:
                target = self.window
                for p in path:
                    target = getattr(target, p)
                result = target(user_text)  # type: ignore
                if isinstance(result, str):
                    return result
                return getattr(result, "text", "") or str(result)
            except Exception:
                continue

        # ========== Fallback: read the latest model output from context ==========
        # If neither path works, read last message from current context.
        try:
            if hasattr(self.window, "ctx") and hasattr(self.window.ctx, "last_output_text"):
                return self.window.ctx.last_output_text()  # hypothetical helper
        except Exception:
            pass

        raise RuntimeError(
            "Unable to bridge into PyGPT. Please wire Adapter A or B to your build."
        )

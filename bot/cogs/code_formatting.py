import ast
import logging
import re
import time
from typing import NamedTuple, Optional, Sequence

import discord
from discord import Embed, Message, RawMessageUpdateEvent
from discord.ext.commands import Bot, Cog

from bot.cogs.token_remover import TokenRemover
from bot.constants import Categories, Channels, DEBUG_MODE
from bot.utils.messages import wait_for_deletion

log = logging.getLogger(__name__)

BACKTICK = "`"
TICKS = {
    BACKTICK,
    "'",
    '"',
    "\u00b4",  # ACUTE ACCENT
    "\u2018",  # LEFT SINGLE QUOTATION MARK
    "\u2019",  # RIGHT SINGLE QUOTATION MARK
    "\u2032",  # PRIME
    "\u201c",  # LEFT DOUBLE QUOTATION MARK
    "\u201d",  # RIGHT DOUBLE QUOTATION MARK
    "\u2033",  # DOUBLE PRIME
    "\u3003",  # VERTICAL KANA REPEAT MARK UPPER HALF
}
RE_CODE_BLOCK = re.compile(
    fr"""
    (?P<ticks>
        (?P<tick>[{''.join(TICKS)}])  # Put all ticks into a character class within a group.
        \2{{2}}                       # Match previous group 2 more times to ensure the same char.
    )
    (?P<lang>[^\W_]+\n)?              # Optionally match a language specifier followed by a newline.
    (?P<code>.+?)                     # Match the actual code within the block.
    \1                                # Match the same 3 ticks used at the start of the block.
    """,
    re.DOTALL | re.VERBOSE
)

PY_LANG_CODES = ("python", "py")
EXAMPLE_PY = f"python\nprint('Hello, world!')"  # Make sure to escape any Markdown symbols here.
EXAMPLE_CODE_BLOCKS = (
    "\\`\\`\\`{content}\n\\`\\`\\`\n\n"
    "**This will result in the following:**\n"
    "```{content}```"
)


class CodeBlock(NamedTuple):
    """Represents a Markdown code block."""

    content: str
    language: str
    tick: str


class CodeFormatting(Cog):
    """Detect improperly formatted code blocks and suggest proper formatting."""

    def __init__(self, bot: Bot):
        self.bot = bot

        # Stores allowed channels plus epoch time since last call.
        self.channel_cooldowns = {
            Channels.python_discussion: 0,
        }

        # These channels will also work, but will not be subject to cooldown
        self.channel_whitelist = (
            Channels.bot_commands,
        )

        # Stores improperly formatted Python codeblock message ids and the corresponding bot message
        self.codeblock_message_ids = {}

    @classmethod
    def get_bad_ticks_message(cls, code_block: CodeBlock) -> Optional[str]:
        """Return instructions on using the correct ticks for `code_block`."""
        valid_ticks = f"\\{BACKTICK}" * 3

        # The space at the end is important here because something may be appended!
        instructions = (
            "It looks like you are trying to paste code into this channel.\n\n"
            "You seem to be using the wrong symbols to indicate where the code block should start. "
            f"The correct symbols would be {valid_ticks}, not `{code_block.tick * 3}`. "
        )

        # Check if the code has an issue with the language specifier.
        addition_msg = cls.get_bad_lang_message(code_block.content)
        if not addition_msg:
            addition_msg = cls.get_no_lang_message(code_block.content)

        # Combine the back ticks message with the language specifier message. The latter will
        # already have an example code block.
        if addition_msg:
            # The first line has a double line break which is not desirable when appending the msg.
            addition_msg = addition_msg.replace("\n\n", " ", 1)

            # Make the first character of the addition lower case.
            instructions += "\n\nFurthermore, " + addition_msg[0].lower() + addition_msg[1:]
        else:
            # Determine the example code to put in the code block based on the language specifier.
            if code_block.language.lower() in PY_LANG_CODES:
                content = EXAMPLE_PY
            elif code_block.language:
                # It's not feasible to determine what would be a valid example for other languages.
                content = f"{code_block.language}\n..."
            else:
                content = "Hello, world!"

            example_blocks = EXAMPLE_CODE_BLOCKS.format(content=content)
            instructions += f"\n\n**Here is an example of how it should look:**\n{example_blocks}"

        return instructions

    @classmethod
    def get_no_ticks_message(cls, content: str) -> Optional[str]:
        """If `content` is Python/REPL code, return instructions on using code blocks."""
        if cls.is_repl_code(content) or cls.is_python_code(content):
            example_blocks = EXAMPLE_CODE_BLOCKS.format(content=EXAMPLE_PY)
            return (
                "It looks like you're trying to paste code into this channel.\n\n"
                "Discord has support for Markdown, which allows you to post code with full "
                "syntax highlighting. Please use these whenever you paste code, as this "
                "helps improve the legibility and makes it easier for us to help you.\n\n"
                f"**To do this, use the following method:**\n{example_blocks}"
            )

    @staticmethod
    def get_bad_lang_message(content: str) -> Optional[str]:
        """
        Return instructions on fixing the Python language specifier for a code block.

        If `content` doesn't start with "python" or "py" as the language specifier, return None.
        """
        stripped = content.lstrip().lower()
        lang = next((lang for lang in PY_LANG_CODES if stripped.startswith(lang)), None)

        if lang:
            # Note that get_bad_ticks_message expects the first line to have an extra newline.
            lines = ["It looks like you incorrectly specified a language for your code block.\n"]

            if content.startswith(" "):
                lines.append(f"Make sure there are no spaces between the back ticks and `{lang}`.")

            if stripped[len(lang)] != "\n":
                lines.append(
                    f"Make sure you put your code on a new line following `{lang}`. "
                    f"There must not be any spaces after `{lang}`."
                )

            example_blocks = EXAMPLE_CODE_BLOCKS.format(content=EXAMPLE_PY)
            lines.append(f"\n**Here is an example of how it should look:**\n{example_blocks}")

            return "\n".join(lines)

    @classmethod
    def get_no_lang_message(cls, content: str) -> Optional[str]:
        """
        Return instructions on specifying a language for a code block.

        If `content` is not valid Python or Python REPL code, return None.
        """
        if cls.is_repl_code(content) or cls.is_python_code(content):
            example_blocks = EXAMPLE_CODE_BLOCKS.format(content=EXAMPLE_PY)

            # Note that get_bad_ticks_message expects the first line to have an extra newline.
            return (
                "It looks like you pasted Python code without syntax highlighting.\n\n"
                "Please use syntax highlighting to improve the legibility of your code and make "
                "it easier for us to help you.\n\n"
                f"**To do this, use the following method:**\n{example_blocks}"
            )

    @staticmethod
    def find_code_blocks(message: str) -> Sequence[CodeBlock]:
        """
        Find and return all Markdown code blocks in the `message`.

        Code blocks with 3 or less lines are excluded.

        If the `message` contains at least one code block with valid ticks and a specified language,
        return an empty sequence. This is based on the assumption that if the user managed to get
        one code block right, they already know how to fix the rest themselves.
        """
        code_blocks = []
        for match in RE_CODE_BLOCK.finditer(message):
            # Used to ensure non-matched groups have an empty string as the default value.
            groups = match.groupdict("")
            language = groups["lang"].strip()  # Strip the newline cause it's included in the group.

            if groups["tick"] == BACKTICK and language:
                return ()
            elif len(groups["code"].split("\n", 3)) > 3:
                code_block = CodeBlock(groups["code"], language, groups["tick"])
                code_blocks.append(code_block)

        return code_blocks

    @staticmethod
    def is_repl_code(content: str, threshold: int = 3) -> bool:
        """Return True if `content` has at least `threshold` number of Python REPL-like lines."""
        repl_lines = 0
        for line in content.splitlines():
            if line.startswith(">>> ") or line.startswith("... "):
                repl_lines += 1

            if repl_lines == threshold:
                return True

        return False

    @staticmethod
    def is_help_channel(channel: discord.TextChannel) -> bool:
        """Return True if `channel` is in one of the help categories."""
        return (
            getattr(channel, "category", None)
            and channel.category.id in (Categories.help_available, Categories.help_in_use)
        )

    def is_on_cooldown(self, channel: discord.TextChannel) -> bool:
        """
        Return True if an embed was sent for `channel` in the last 300 seconds.

        Note: only channels in the `channel_cooldowns` have cooldowns enabled.
        """
        return (time.time() - self.channel_cooldowns.get(channel.id, 0)) < 300

    @staticmethod
    def is_python_code(content: str) -> bool:
        """Return True if `content` is valid Python consisting of more than just expressions."""
        try:
            # Attempt to parse the message into an AST node.
            # Invalid Python code will raise a SyntaxError.
            tree = ast.parse(content)
        except SyntaxError:
            log.trace("Code is not valid Python.")
            return False

        # Multiple lines of single words could be interpreted as expressions.
        # This check is to avoid all nodes being parsed as expressions.
        # (e.g. words over multiple lines)
        if not all(isinstance(node, ast.Expr) for node in tree.body):
            return True
        else:
            log.trace("Code consists only of expressions.")
            return False

    def is_valid_channel(self, channel: discord.TextChannel) -> bool:
        """Return True if `channel` is a help channel, may be on cooldown, or is whitelisted."""
        return (
            self.is_help_channel(channel)
            or channel.id in self.channel_cooldowns
            or channel.id in self.channel_whitelist
        )

    async def send_guide_embed(self, message: discord.Message, description: str) -> None:
        """
        Send an embed with `description` as a guide for an improperly formatted `message`.

        The embed will be deleted automatically after 5 minutes.
        """
        embed = Embed(description=description)
        bot_message = await message.channel.send(f"Hey {message.author.mention}!", embed=embed)
        self.codeblock_message_ids[message.id] = bot_message.id

        self.bot.loop.create_task(
            wait_for_deletion(bot_message, user_ids=(message.author.id,), client=self.bot)
        )

    def should_parse(self, message: discord.Message) -> bool:
        """
        Return True if `message` should be parsed.

        A qualifying message:

        1. Is not authored by a bot
        2. Is in a valid channel
        3. Has more than 3 lines
        4. Has no bot token
        """
        return (
            not message.author.bot
            and self.is_valid_channel(message.channel)
            and len(message.content.split("\n", 3)) > 3
            and not TokenRemover.find_token_in_message(message)
        )

    @Cog.listener()
    async def on_message(self, msg: Message) -> None:
        """Detect incorrect Markdown code blocks in `msg` and send instructions to fix them."""
        if not self.should_parse(msg):
            return

        # When debugging, ignore cooldowns.
        if self.is_on_cooldown(msg.channel) and not DEBUG_MODE:
            return

        blocks = self.find_code_blocks(msg.content)
        if not blocks:
            # No code blocks found in the message.
            description = self.get_no_ticks_message(msg.content)
        else:
            # Get the first code block with invalid ticks.
            block = next((block for block in blocks if block.tick != BACKTICK), None)

            if block:
                # A code block exists but has invalid ticks.
                description = self.get_bad_ticks_message(block)
            else:
                # Only other possibility is a block with valid ticks but a missing language.
                block = blocks[0]

                # Check for a bad language first to avoid parsing content into an AST.
                description = self.get_bad_lang_message(block.content)
                if not description:
                    description = self.get_no_lang_message(block.content)

        if description:
            await self.send_guide_embed(msg, description)
            if msg.channel.id not in self.channel_whitelist:
                self.channel_cooldowns[msg.channel.id] = time.time()

    @Cog.listener()
    async def on_raw_message_edit(self, payload: RawMessageUpdateEvent) -> None:
        """Delete the instructions message if an edited message had its code blocks fixed."""
        if (
            # Checks to see if the message was called out by the bot
            payload.message_id not in self.codeblock_message_ids
            # Makes sure that there is content in the message
            or payload.data.get("content") is None
            # Makes sure there's a channel id in the message payload
            or payload.data.get("channel_id") is None
        ):
            return

        # Parse the message to see if the code blocks have been fixed.
        code_blocks = self.find_code_blocks(payload.data.get("content"))

        # If the message is fixed, delete the bot message and the entry from the id dictionary.
        if not code_blocks:
            log.trace("User's incorrect code block has been fixed. Removing bot formatting message.")

            channel = self.bot.get_channel(int(payload.data.get("channel_id")))
            bot_message = await channel.fetch_message(self.codeblock_message_ids[payload.message_id])

            await bot_message.delete()
            del self.codeblock_message_ids[payload.message_id]


def setup(bot: Bot) -> None:
    """Load the CodeFormatting cog."""
    bot.add_cog(CodeFormatting(bot))

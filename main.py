import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import io
import asyncio
from datetime import datetime, UTC

CONFIG_FILE = "config.json"
TICKETS_FILE = "tickets.json"

ticket_creation_locks: set[int] = set()


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def load_config():
    return load_json(CONFIG_FILE, {
        "token": "",
        "category_id": None,
        "closed_category_id": None,
        "support_role_ids": [],
        "log_channel_id": None,
        "panel_title": "Clothing Tickets",
        "panel_description": "Press the button below to create a ticket.",
        "panel_button_text": "Create Ticket",
        "panel_button_color": "blurple",
        "ticket_welcome_message": "Welcome to your ticket. Please say what clothing you want to buy.",
        "ticket_name_format": "ticket-{number}",
        "allow_multiple_tickets": False,
        "ticket_counter": 0
    })


def save_config(data):
    save_json(CONFIG_FILE, data)


def load_tickets():
    return load_json(TICKETS_FILE, {})


def save_tickets(data):
    save_json(TICKETS_FILE, data)


def style_from_name(name: str) -> discord.ButtonStyle:
    name = (name or "blurple").lower()
    styles = {
        "blurple": discord.ButtonStyle.blurple,
        "blue": discord.ButtonStyle.blurple,
        "gray": discord.ButtonStyle.gray,
        "grey": discord.ButtonStyle.gray,
        "green": discord.ButtonStyle.green,
        "red": discord.ButtonStyle.red,
    }
    return styles.get(name, discord.ButtonStyle.blurple)


def sanitize_channel_name(text: str) -> str:
    text = text.lower().strip().replace(" ", "-")
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    cleaned = "".join(ch for ch in text if ch in allowed)

    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")

    cleaned = cleaned.strip("-")
    return cleaned[:90] if cleaned else "ticket"


def build_ticket_name(config: dict, user: discord.abc.User) -> str:
    config["ticket_counter"] = config.get("ticket_counter", 0) + 1
    save_config(config)

    ticket_number = config["ticket_counter"]
    padded_number = f"{ticket_number:04d}"

    template = config.get("ticket_name_format", "ticket-{number}")
    raw_name = template.format(
        user=getattr(user, "name", "user"),
        display=getattr(user, "display_name", getattr(user, "name", "user")),
        id=getattr(user, "id", 0),
        number=padded_number
    )
    return sanitize_channel_name(raw_name)


def get_support_roles(guild: discord.Guild, role_ids: list[int]) -> list[discord.Role]:
    roles = []
    for role_id in role_ids:
        role = guild.get_role(role_id)
        if role:
            roles.append(role)
    return roles


def build_panel_embed(config: dict) -> discord.Embed:
    return discord.Embed(
        title=config.get("panel_title", "Clothing Tickets"),
        description=config.get("panel_description", "Press the button below to create a ticket."),
        color=discord.Color.blurple()
    )


def build_ticket_embed(message: str, claimed_by: str | None = None, closed: bool = False) -> discord.Embed:
    embed = discord.Embed(
        title="Ticket",
        description=message,
        color=discord.Color.red() if closed else discord.Color.green()
    )
    embed.add_field(name="Status", value="Closed" if closed else "Open", inline=False)
    if claimed_by:
        embed.add_field(name="Claimed By", value=claimed_by, inline=False)
    return embed


intents = discord.Intents.default()
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)


class ClosedTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Reopen Ticket", style=discord.ButtonStyle.green, custom_id="ticket_reopen_button")
    async def reopen_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This only works in a server ticket channel.", ephemeral=True)
            return

        tickets = load_tickets()
        channel_id_str = str(interaction.channel.id)

        if channel_id_str not in tickets:
            await interaction.response.send_message("This channel is not registered as a ticket.", ephemeral=True)
            return

        config = load_config()
        support_roles = get_support_roles(interaction.guild, config.get("support_role_ids", []))

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if support_roles and member is not None:
            if not any(role in member.roles for role in support_roles):
                await interaction.response.send_message("You do not have permission to reopen this ticket.", ephemeral=True)
                return

        open_category_id = config.get("category_id")
        open_category = interaction.guild.get_channel(open_category_id) if open_category_id else None

        if open_category is None or not isinstance(open_category, discord.CategoryChannel):
            await interaction.response.send_message("Open ticket category is not set correctly.", ephemeral=True)
            return

        ticket_info = tickets[channel_id_str]
        ticket_info["closed"] = False
        save_tickets(tickets)

        await interaction.channel.edit(category=open_category, reason=f"Ticket reopened by {interaction.user}")

        mention_text = f"<@{ticket_info['owner_id']}>"
        if support_roles:
            mention_text += " " + " ".join(role.mention for role in support_roles)

        embed = build_ticket_embed(
            "This ticket has been reopened.",
            claimed_by=(f"<@{ticket_info['claimed_by']}>" if ticket_info.get("claimed_by") else None),
            closed=False
        )

        await interaction.response.send_message("Ticket reopened.", ephemeral=True)
        await interaction.channel.send(content=mention_text, embed=embed, view=OpenTicketView())

        log_channel_id = config.get("log_channel_id")
        log_channel = interaction.guild.get_channel(log_channel_id) if log_channel_id else None
        if isinstance(log_channel, discord.TextChannel):
            await log_channel.send(f"♻️ Ticket reopened: {interaction.channel.mention} by {interaction.user.mention}")

    @discord.ui.button(label="Delete Ticket", style=discord.ButtonStyle.red, custom_id="ticket_delete_button")
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This only works in a server ticket channel.", ephemeral=True)
            return

        tickets = load_tickets()
        channel_id_str = str(interaction.channel.id)

        if channel_id_str not in tickets:
            await interaction.response.send_message("This channel is not registered as a ticket.", ephemeral=True)
            return

        config = load_config()
        support_roles = get_support_roles(interaction.guild, config.get("support_role_ids", []))

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if support_roles and member is not None:
            if not any(role in member.roles for role in support_roles):
                await interaction.response.send_message("You do not have permission to delete this ticket.", ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True)

        ticket_info = tickets[channel_id_str]

        log_channel_id = config.get("log_channel_id")
        log_channel = interaction.guild.get_channel(log_channel_id) if log_channel_id else None

        if isinstance(log_channel, discord.TextChannel):
            owner_id = ticket_info.get("owner_id")
            owner_mention = f"<@{owner_id}>" if owner_id else "Unknown"
            await log_channel.send(
                f"🗑️ Ticket deleted: `{interaction.channel.name}` | User: {owner_mention} | Deleted by: {interaction.user.mention}"
            )

        del tickets[channel_id_str]
        save_tickets(tickets)

        await interaction.channel.delete(reason=f"Ticket deleted by {interaction.user}")


class OpenTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim Ticket", style=discord.ButtonStyle.gray, custom_id="ticket_claim_button")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This only works in a server ticket channel.", ephemeral=True)
            return

        tickets = load_tickets()
        channel_id_str = str(interaction.channel.id)

        if channel_id_str not in tickets:
            await interaction.response.send_message("This channel is not registered as a ticket.", ephemeral=True)
            return

        ticket_info = tickets[channel_id_str]
        config = load_config()
        support_roles = get_support_roles(interaction.guild, config.get("support_role_ids", []))

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if support_roles and member is not None:
            if not any(role in member.roles for role in support_roles):
                await interaction.response.send_message("You do not have permission to claim this ticket.", ephemeral=True)
                return

        if ticket_info.get("closed"):
            await interaction.response.send_message("This ticket is closed.", ephemeral=True)
            return

        if ticket_info.get("claimed_by") == interaction.user.id:
            await interaction.response.send_message("You already claimed this ticket.", ephemeral=True)
            return

        ticket_info["claimed_by"] = interaction.user.id
        save_tickets(tickets)

        embed = build_ticket_embed(
            "This ticket is now being handled.",
            claimed_by=interaction.user.mention,
            closed=False
        )

        await interaction.response.send_message("Ticket claimed.", ephemeral=True)
        await interaction.channel.send(embed=embed)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="ticket_close_button")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This only works in a server ticket channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        tickets = load_tickets()
        channel_id_str = str(interaction.channel.id)

        if channel_id_str not in tickets:
            await interaction.followup.send("This channel is not registered as a ticket.", ephemeral=True)
            return

        ticket_info = tickets[channel_id_str]
        config = load_config()

        closed_category_id = config.get("closed_category_id")
        closed_category = interaction.guild.get_channel(closed_category_id) if closed_category_id else None

        if closed_category is None or not isinstance(closed_category, discord.CategoryChannel):
            await interaction.followup.send(
                "Closed ticket category is not set. Use /setclosedcategory first.",
                ephemeral=True
            )
            return

        transcript_lines = [
            f"Ticket Channel: #{interaction.channel.name}",
            f"Guild: {interaction.guild.name}",
            f"Opened By User ID: {ticket_info.get('owner_id')}",
            f"Claimed By User ID: {ticket_info.get('claimed_by')}",
            f"Closed By: {interaction.user} ({interaction.user.id})",
            f"Closed At UTC: {datetime.now(UTC).isoformat()}",
            "-" * 60
        ]

        async for msg in interaction.channel.history(limit=None, oldest_first=True):
            created = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            author = f"{msg.author} ({msg.author.id})"
            content = msg.content if msg.content else ""

            attachment_text = ""
            if msg.attachments:
                attachment_urls = ", ".join(a.url for a in msg.attachments)
                attachment_text = f" [Attachments: {attachment_urls}]"

            transcript_lines.append(f"[{created}] {author}: {content}{attachment_text}")

        transcript_text = "\n".join(transcript_lines)
        transcript_filename = f"transcript-{interaction.channel.name}.txt"

        log_channel_id = config.get("log_channel_id")
        log_channel = interaction.guild.get_channel(log_channel_id) if log_channel_id else None

        if isinstance(log_channel, discord.TextChannel):
            transcript_file = discord.File(
                io.BytesIO(transcript_text.encode("utf-8")),
                filename=transcript_filename
            )

            owner_id = ticket_info.get("owner_id")
            owner_mention = f"<@{owner_id}>" if owner_id else "Unknown"
            claimed_mention = f"<@{ticket_info['claimed_by']}>" if ticket_info.get("claimed_by") else "Nobody"

            await log_channel.send(
                content=(
                    f"🧾 **Ticket Closed**\n"
                    f"User: {owner_mention}\n"
                    f"Claimed By: {claimed_mention}\n"
                    f"Channel Name: `{interaction.channel.name}`\n"
                    f"Closed By: {interaction.user.mention}"
                ),
                file=transcript_file
            )

        ticket_info["closed"] = True
        save_tickets(tickets)

        await interaction.channel.edit(category=closed_category, reason=f"Ticket closed by {interaction.user}")

        embed = build_ticket_embed(
            "This ticket has been closed. Use the buttons below if needed.",
            claimed_by=(f"<@{ticket_info['claimed_by']}>" if ticket_info.get("claimed_by") else None),
            closed=True
        )

        await interaction.channel.send(embed=embed, view=ClosedTicketView())
        await interaction.followup.send("Ticket closed.", ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self, button_text: str, button_color: str):
        super().__init__(timeout=None)

        button = discord.ui.Button(
            label=button_text,
            style=style_from_name(button_color),
            custom_id="ticket_create_button"
        )
        button.callback = self.create_ticket
        self.add_item(button)

    async def create_ticket(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        user_id = interaction.user.id

        if user_id in ticket_creation_locks:
            await interaction.response.send_message(
                "Your ticket is already being created. Give it a second.",
                ephemeral=True
            )
            return

        ticket_creation_locks.add(user_id)

        try:
            config = load_config()
            tickets = load_tickets()

            category_id = config.get("category_id")
            support_role_ids = config.get("support_role_ids", [])
            allow_multiple_tickets = config.get("allow_multiple_tickets", False)

            if not category_id:
                await interaction.response.send_message("Ticket category has not been set yet.", ephemeral=True)
                return

            category = interaction.guild.get_channel(category_id)
            if category is None or not isinstance(category, discord.CategoryChannel):
                await interaction.response.send_message("The saved ticket category is invalid.", ephemeral=True)
                return

            if not allow_multiple_tickets:
                for channel_id, info in tickets.items():
                    if info.get("owner_id") == user_id and not info.get("closed", False):
                        existing_channel = interaction.guild.get_channel(int(channel_id))
                        if existing_channel:
                            await interaction.response.send_message(
                                f"You already have an open ticket: {existing_channel.mention}",
                                ephemeral=True
                            )
                            return

                categories_to_check = []
                open_category = interaction.guild.get_channel(config.get("category_id")) if config.get("category_id") else None
                closed_category = interaction.guild.get_channel(config.get("closed_category_id")) if config.get("closed_category_id") else None

                if isinstance(open_category, discord.CategoryChannel):
                    categories_to_check.append(open_category)
                if isinstance(closed_category, discord.CategoryChannel):
                    categories_to_check.append(closed_category)

                for cat in categories_to_check:
                    for channel in cat.text_channels:
                        overwrites = channel.overwrites_for(interaction.user)
                        if overwrites.view_channel:
                            if str(channel.id) not in tickets:
                                tickets[str(channel.id)] = {
                                    "owner_id": user_id,
                                    "created_at": datetime.now(UTC).isoformat(),
                                    "channel_name": channel.name,
                                    "claimed_by": None,
                                    "closed": (cat.id == config.get("closed_category_id"))
                                }
                                save_tickets(tickets)

                            if cat.id == config.get("category_id"):
                                await interaction.response.send_message(
                                    f"You already have an open ticket: {channel.mention}",
                                    ephemeral=True
                                )
                                return

            await interaction.response.defer(ephemeral=True)

            channel_name = build_ticket_name(config, interaction.user)

            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True
                ),
                interaction.guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_channels=True,
                    read_message_history=True
                )
            }

            support_roles = get_support_roles(interaction.guild, support_role_ids)
            for role in support_roles:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True
                )

            ticket_channel = await interaction.guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Ticket created by {interaction.user}"
            )

            tickets[str(ticket_channel.id)] = {
                "owner_id": user_id,
                "created_at": datetime.now(UTC).isoformat(),
                "channel_name": ticket_channel.name,
                "claimed_by": None,
                "closed": False
            }
            save_tickets(tickets)

            welcome_message = config.get(
                "ticket_welcome_message",
                "Welcome to your ticket. Please say what clothing you want to buy."
            )

            mention_text = interaction.user.mention
            if support_roles:
                mention_text += " " + " ".join(role.mention for role in support_roles)

            embed = build_ticket_embed(welcome_message, claimed_by=None, closed=False)

            await ticket_channel.send(content=mention_text, embed=embed, view=OpenTicketView())

            log_channel_id = config.get("log_channel_id")
            log_channel = interaction.guild.get_channel(log_channel_id) if log_channel_id else None
            if isinstance(log_channel, discord.TextChannel):
                await log_channel.send(f"📩 Ticket opened: {ticket_channel.mention} by {interaction.user.mention}")

            await interaction.followup.send(
                f"Your ticket has been created: {ticket_channel.mention}",
                ephemeral=True
            )

        finally:
            ticket_creation_locks.discard(user_id)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s).")
    except Exception as e:
        print(f"Command sync failed: {e}")

    print(f"Logged in as {bot.user}")


async def setup_views():
    config = load_config()
    bot.add_view(TicketPanelView(
        config.get("panel_button_text", "Create Ticket"),
        config.get("panel_button_color", "blurple")
    ))
    bot.add_view(OpenTicketView())
    bot.add_view(ClosedTicketView())


async def setup_hook():
    await setup_views()


bot.setup_hook = setup_hook


@bot.tree.command(name="makepanel", description="Post the ticket panel")
@app_commands.checks.has_permissions(administrator=True)
async def makepanel(interaction: discord.Interaction):
    config = load_config()
    embed = build_panel_embed(config)
    view = TicketPanelView(
        config.get("panel_button_text", "Create Ticket"),
        config.get("panel_button_color", "blurple")
    )

    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("Ticket panel created.", ephemeral=True)


@bot.tree.command(name="setcategory", description="Set the category where open tickets are created")
@app_commands.checks.has_permissions(administrator=True)
async def setcategory(interaction: discord.Interaction, category: discord.CategoryChannel):
    config = load_config()
    config["category_id"] = category.id
    save_config(config)
    await interaction.response.send_message(f"Open ticket category set to **{category.name}**.", ephemeral=True)


@bot.tree.command(name="setclosedcategory", description="Set the category where closed tickets go")
@app_commands.checks.has_permissions(administrator=True)
async def setclosedcategory(interaction: discord.Interaction, category: discord.CategoryChannel):
    config = load_config()
    config["closed_category_id"] = category.id
    save_config(config)
    await interaction.response.send_message(f"Closed ticket category set to **{category.name}**.", ephemeral=True)


@bot.tree.command(name="setroles", description="Set up to 5 roles that can see tickets")
@app_commands.checks.has_permissions(administrator=True)
async def setroles(
    interaction: discord.Interaction,
    role1: discord.Role,
    role2: discord.Role | None = None,
    role3: discord.Role | None = None,
    role4: discord.Role | None = None,
    role5: discord.Role | None = None
):
    roles = [r for r in [role1, role2, role3, role4, role5] if r is not None]
    config = load_config()
    config["support_role_ids"] = [r.id for r in roles]
    save_config(config)
    await interaction.response.send_message(
        f"Support roles set to: **{', '.join(r.name for r in roles)}**",
        ephemeral=True
    )


@bot.tree.command(name="setlogchannel", description="Set the channel for ticket logs and transcripts")
@app_commands.checks.has_permissions(administrator=True)
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    config = load_config()
    config["log_channel_id"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"Log channel set to {channel.mention}.", ephemeral=True)


@bot.tree.command(name="setpaneltitle", description="Set the panel title")
@app_commands.checks.has_permissions(administrator=True)
async def setpaneltitle(interaction: discord.Interaction, title: str):
    config = load_config()
    config["panel_title"] = title
    save_config(config)
    await interaction.response.send_message(f"Panel title updated to: **{title}**", ephemeral=True)


@bot.tree.command(name="setpaneldescription", description="Set the panel description")
@app_commands.checks.has_permissions(administrator=True)
async def setpaneldescription(interaction: discord.Interaction, description: str):
    config = load_config()
    config["panel_description"] = description
    save_config(config)
    await interaction.response.send_message("Panel description updated.", ephemeral=True)


@bot.tree.command(name="setpanelbutton", description="Set the panel button text and color")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(color="blurple, green, gray, or red")
async def setpanelbutton(interaction: discord.Interaction, text: str, color: str):
    config = load_config()
    config["panel_button_text"] = text
    config["panel_button_color"] = color.lower()
    save_config(config)
    await interaction.response.send_message(
        f"Panel button updated to **{text}** with color **{color.lower()}**.",
        ephemeral=True
    )


@bot.tree.command(name="setticketmessage", description="Set the message sent inside new tickets")
@app_commands.checks.has_permissions(administrator=True)
async def setticketmessage(interaction: discord.Interaction, message: str):
    config = load_config()
    config["ticket_welcome_message"] = message
    save_config(config)
    await interaction.response.send_message("Ticket welcome message updated.", ephemeral=True)


@bot.tree.command(name="setticketname", description="Set the ticket channel name format")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(format_text="Use {user}, {display}, {id}, and {number}. Example: order-{number}")
async def setticketname(interaction: discord.Interaction, format_text: str):
    config = load_config()
    config["ticket_name_format"] = format_text
    save_config(config)
    await interaction.response.send_message(
        f"Ticket naming format set to: `{format_text}`",
        ephemeral=True
    )


@bot.tree.command(name="setmultipletickets", description="Allow or block users from opening multiple tickets")
@app_commands.checks.has_permissions(administrator=True)
async def setmultipletickets(interaction: discord.Interaction, allow: bool):
    config = load_config()
    config["allow_multiple_tickets"] = allow
    save_config(config)
    await interaction.response.send_message(f"Allow multiple tickets set to **{allow}**.", ephemeral=True)


@bot.tree.command(name="panelpreview", description="Preview your current ticket panel")
@app_commands.checks.has_permissions(administrator=True)
async def panelpreview(interaction: discord.Interaction):
    config = load_config()
    embed = build_panel_embed(config)
    view = TicketPanelView(
        config.get("panel_button_text", "Create Ticket"),
        config.get("panel_button_color", "blurple")
    )
    await interaction.response.send_message(
        "Here is your current panel preview:",
        embed=embed,
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="ticketsettings", description="Show current ticket settings")
@app_commands.checks.has_permissions(administrator=True)
async def ticketsettings(interaction: discord.Interaction):
    config = load_config()

    category_text = "Not set"
    if config.get("category_id") and interaction.guild:
        category = interaction.guild.get_channel(config["category_id"])
        if category:
            category_text = category.name

    closed_category_text = "Not set"
    if config.get("closed_category_id") and interaction.guild:
        category = interaction.guild.get_channel(config["closed_category_id"])
        if category:
            closed_category_text = category.name

    log_text = "Not set"
    if config.get("log_channel_id") and interaction.guild:
        log_channel = interaction.guild.get_channel(config["log_channel_id"])
        if log_channel:
            log_text = log_channel.name

    role_names = []
    if interaction.guild:
        for role_id in config.get("support_role_ids", []):
            role = interaction.guild.get_role(role_id)
            if role:
                role_names.append(role.name)

    embed = discord.Embed(title="Ticket Settings", color=discord.Color.blurple())
    embed.add_field(name="Open Category", value=category_text, inline=False)
    embed.add_field(name="Closed Category", value=closed_category_text, inline=False)
    embed.add_field(name="Support Roles", value=", ".join(role_names) if role_names else "None", inline=False)
    embed.add_field(name="Log Channel", value=log_text, inline=False)
    embed.add_field(name="Panel Title", value=config.get("panel_title", "Clothing Tickets"), inline=False)
    embed.add_field(name="Panel Description", value=config.get("panel_description", "None"), inline=False)
    embed.add_field(name="Button Text", value=config.get("panel_button_text", "Create Ticket"), inline=False)
    embed.add_field(name="Button Color", value=config.get("panel_button_color", "blurple"), inline=False)
    embed.add_field(name="Ticket Welcome Message", value=config.get("ticket_welcome_message", "None"), inline=False)
    embed.add_field(name="Ticket Name Format", value=config.get("ticket_name_format", "ticket-{number}"), inline=False)
    embed.add_field(name="Allow Multiple Tickets", value=str(config.get("allow_multiple_tickets", False)), inline=False)
    embed.add_field(name="Ticket Counter", value=str(config.get("ticket_counter", 0)), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@setcategory.error
@setclosedcategory.error
@setroles.error
@setlogchannel.error
@setpaneltitle.error
@setpaneldescription.error
@setpanelbutton.error
@setticketmessage.error
@setticketname.error
@setmultipletickets.error
@makepanel.error
@panelpreview.error
@ticketsettings.error
async def admin_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        message = "You need administrator permission to use that command."
    else:
        message = f"Something went wrong: {error}"

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


import os

config = load_config()
token = os.getenv("TOKEN") or config["token"]

bot.run(token)

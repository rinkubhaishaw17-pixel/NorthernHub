
import discord
from discord.ext import commands, tasks
from discord import app_commands
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, UTC
import os
import json
import io
import traceback
import asyncio
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- RENDER HEALTH CHECK SERVER ---
# This small web server is required by Render to know that the bot is running.
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# --- DISCORD BOT SETUP ---
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- CONFIGURABLE CONSTANTS ---
LOG_CHANNEL_NAME = "ticket-logs"
TRANSCRIPT_CHANNEL_NAME = "transcripts"
VOUCH_CHANNEL_NAME = "vouches"
TICKET_CATEGORY_NAME = "📋 Tickets"
SPREADSHEET_NAME = "ProductKeys"
CREDENTIALS_FILE = "google-credentials.json"
MOD_LOG_CHANNEL_NAME = "moderation-logs"
WELCOME_CHANNEL_NAME = "welcome"
STATS_CATEGORY_NAME = "📊 Server Stats"
MEMBER_COUNT_CHANNEL_NAME = "Members: {count}"
BOT_COUNT_CHANNEL_NAME = "Bots: {count}"
BACKUP_FOLDER = "backups"
GIVEAWAY_CHANNEL_NAME = "giveaways"
GIVEAWAY_LOGS_CHANNEL_NAME = "giveaway-logs"

# --- DATA STORAGE ---
def load_data(filename, default_value={}):
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"❌ Error decoding {filename}. Using default value.")
            return default_value
    return default_value

def save_data(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f, indent=4)

warnings_data = load_data("warnings.json")
auto_roles_data = load_data("auto_roles.json")
afk_data = load_data("afk_status.json")
giveaways_data = load_data("giveaways.json")
templates_data = load_data("templates.json")
vouch_data = load_data("vouch_data.json")

# --- GOOGLE SHEETS SETUP ---
try:
    if "GOOGLE_CREDENTIALS_JSON" in os.environ:
        creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
        scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=scope)
    else:
        scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scope)
        
    client = gspread.authorize(creds)
    sheet = client.open(SPREADSHEET_NAME).sheet1
    SHEETS_ENABLED = True
    print("✅ Google Sheets connection established")
except Exception as e:
    print(f"❌ Google Sheets connection failed: {e}")
    print("⚠️  Running in limited mode without Google Sheets functionality")
    SHEETS_ENABLED = False
    sheet = None

def validate_sheet_columns():
    if not SHEETS_ENABLED or not sheet:
        return False
    try:
        required = {"Product", "Key", "Used", "User"}
        headers = set(sheet.row_values(1))
        missing = required - headers
        if missing:
            print(f"❌ Spreadsheet missing required columns: {', '.join(missing)}")
            return False
        return True
    except Exception as e:
        print(f"❌ Error validating sheet columns: {e}")
        return False

def get_key(product_name, user_tag):
    if not SHEETS_ENABLED or not sheet:
        return None
    try:
        headers = sheet.row_values(1)
        required_columns = ["Product", "Key", "Used", "User"]
        for col in required_columns:
            if col not in headers:
                print(f"❌ Missing column in spreadsheet: '{col}'")
                return None
        product_col = headers.index("Product") + 1
        key_col = headers.index("Key") + 1
        used_col = headers.index("Used") + 1
        user_col = headers.index("User") + 1
    except Exception as e:
        print(f"❌ Error accessing sheet headers: {e}")
        return None
    try:
        records = sheet.get_all_records()
        for i, row in enumerate(records, start=2):
            if row.get("Product", "").lower() == product_name.lower() and row.get("Used", "").lower() != "yes":
                try:
                    sheet.update_cell(i, used_col, "Yes")
                    sheet.update_cell(i, user_col, user_tag)
                    return row.get("Key")
                except Exception as e:
                    print(f"❌ Error updating sheet row {i}: {e}")
                    return None
    except Exception as e:
        print(f"❌ Error retrieving records: {e}")
    return None

def get_stock_summary():
    if not SHEETS_ENABLED or not sheet:
        return {"Error": "Sheets not available"}
    try:
        summary = {}
        records = sheet.get_all_records()
        for row in records:
            if row.get("Used", "").lower() != "yes":
                product = row.get("Product")
                summary[product] = summary.get(product, 0) + 1
        return summary
    except Exception as e:
        print(f"❌ Error getting stock summary: {e}")
        return {"Error": f"Failed to get stock: {e}"}

# --- HELPER FUNCTIONS ---
async def find_or_create_channel(guild, channel_name, category_name=None):
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if not channel:
        category = discord.utils.get(guild.categories, name=category_name) if category_name else None
        channel = await guild.create_text_channel(channel_name, category=category)
    return channel

async def log_to_channel(guild, message, channel_name):
    channel = await find_or_create_channel(guild, channel_name)
    await channel.send(message)

def create_embed(title, description, color, fields=None, thumbnail=None):
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(UTC))
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    return embed

def parse_duration(duration_str):
    """Parses a duration string (e.g., '1d', '5h', '30m', '1d 5h 30m') into a timedelta object."""
    duration_regex = re.compile(r'(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?', re.IGNORECASE)
    match = duration_regex.match(duration_str)
    if not match:
        return None
    
    days, hours, minutes, seconds = [int(x) if x else 0 for x in match.groups()]
    if not any([days, hours, minutes, seconds]):
        return None
        
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)

async def end_giveaway_logic(guild, giveaway_info):
    """Handles the logic for ending a giveaway, including selecting winners and updating messages."""
    channel_id = giveaway_info['channel_id']
    message_id = giveaway_info['message_id']
    prize = giveaway_info['prize']
    winner_count = giveaway_info['winner_count']
    entries = giveaway_info['entries']
    
    try:
        giveaway_channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        giveaway_message = await giveaway_channel.fetch_message(message_id)

        if len(entries) == 0:
            end_embed = create_embed(
                "🎉 Giveaway Ended",
                f"The giveaway for **{prize}** has ended with no entries.",
                discord.Color.red()
            )
            await giveaway_message.edit(embed=end_embed, view=None)
            await giveaway_channel.send(f"❌ The giveaway for **{prize}** has ended with no entries.")
        else:
            winners = random.sample(entries, min(winner_count, len(entries)))
            winner_mentions = [f"<@{uid}>" for uid in winners]
            
            end_embed = create_embed(
                "🎉 GIVEAWAY ENDED! 🎉",
                f"The giveaway for **{prize}** has ended.",
                discord.Color.green()
            )
            end_embed.add_field(name="Winners", value="\n".join(winner_mentions), inline=False)
            end_embed.add_field(name="Prize", value=prize, inline=False)
            await giveaway_message.edit(embed=end_embed, view=None)
            await giveaway_channel.send(f"🎉 Congratulations to the winners: {', '.join(winner_mentions)}! You have won **{prize}**!")
            
        if str(guild.id) in giveaways_data and str(message_id) in giveaways_data[str(guild.id)]:
            del giveaways_data[str(guild.id)][str(message_id)]
            save_data("giveaways.json", giveaways_data)

    except (discord.NotFound, discord.HTTPException) as e:
        print(f"❌ Failed to end giveaway {message_id}: {e}. Removing from storage.")
        if str(guild.id) in giveaways_data and str(message_id) in giveaways_data[str(guild.id)]:
            del giveaways_data[str(guild.id)][str(message_id)]
            save_data("giveaways.json", giveaways_data)

# --- TASKS ---
@tasks.loop(minutes=1)
async def check_temp_bans():
    for guild in bot.guilds:
        try:
            async for ban_entry in guild.bans():
                ban_reason = ban_entry.reason
                if isinstance(ban_reason, str) and ban_reason.startswith("Tempban until:"):
                    end_time_str = ban_reason.split("Tempban until: ")[1].split(" | Reason:")[0].strip()
                    end_time = datetime.fromisoformat(end_time_str)
                    if datetime.now(UTC) >= end_time:
                        await guild.unban(ban_entry.user, reason="Tempban expired")
                        log_channel = await find_or_create_channel(guild, MOD_LOG_CHANNEL_NAME)
                        await log_channel.send(embed=create_embed(
                            "✅ Tempban Expired",
                            f"User `{ban_entry.user}`'s tempban has automatically expired.",
                            discord.Color.green()
                        ))
        except discord.Forbidden:
            print(f"❌ Missing permissions to manage bans in guild {guild.name}")
        except Exception as e:
            print(f"❌ Error in check_temp_bans for guild {guild.name}: {e}")

@tasks.loop(minutes=1)
async def check_giveaways():
    for guild in bot.guilds:
        guild_id_str = str(guild.id)
        if guild_id_str not in giveaways_data:
            continue
        
        expired_giveaways = []
        for message_id, giveaway_info in giveaways_data[guild_id_str].items():
            end_time = datetime.fromisoformat(giveaway_info['end_time'])
            if datetime.now(UTC) >= end_time:
                expired_giveaways.append(giveaway_info)
        
        for giveaway_info in expired_giveaways:
            await end_giveaway_logic(guild, giveaway_info)

@tasks.loop(minutes=30)
async def backup_data_task():
    try:
        if not os.path.exists(BACKUP_FOLDER):
            os.makedirs(BACKUP_FOLDER)
        
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        
        save_data(os.path.join(BACKUP_FOLDER, f"warnings_backup_{timestamp}.json"), warnings_data)
        save_data(os.path.join(BACKUP_FOLDER, f"auto_roles_backup_{timestamp}.json"), auto_roles_data)
        save_data(os.path.join(BACKUP_FOLDER, f"giveaways_backup_{timestamp}.json"), giveaways_data)
        save_data(os.path.join(BACKUP_FOLDER, f"vouch_data_backup_{timestamp}.json"), vouch_data)

        print("✅ Data backup complete.")
    except Exception as e:
        print(f"❌ Failed to perform data backup: {e}")

# --- VIEWS & DROPDOWNS ---
class VouchModal(discord.ui.Modal, title="Submit a Vouch"):
    def __init__(self, prefilled_product=None):
        super().__init__()
        self.prefilled_product = prefilled_product
        self.product_input = discord.ui.TextInput(
            label="Product Name",
            placeholder="e.g., ProductKey 1",
            required=True,
            default=prefilled_product
        )
        self.experience_input = discord.ui.TextInput(
            label="Your Experience",
            placeholder="e.g., The seller was fast and helpful!",
            style=discord.TextStyle.paragraph,
            required=True
        )
        self.rating_input = discord.ui.TextInput(
            label="Star Rating (1-5)",
            placeholder="e.g., 5",
            required=True
        )
        self.supporter_input = discord.ui.TextInput(
            label="Staff Member (optional)",
            placeholder="e.g., JohnDoe",
            required=False
        )
        self.add_item(self.product_input)
        self.add_item(self.experience_input)
        self.add_item(self.rating_input)
        self.add_item(self.supporter_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            rating_int = int(self.rating_input.value)
            if rating_int < 1 or rating_int > 5:
                await interaction.response.send_message("⭐ Rating must be between 1 and 5.", ephemeral=True)
                return
            
            user_id = str(interaction.user.id)
            if user_id not in vouch_data:
                vouch_data[user_id] = {"count": 0, "vouches": []}
            
            vouch_data[user_id]["count"] += 1
            vouch_data[user_id]["vouches"].append({
                "product": self.product_input.value,
                "experience": self.experience_input.value,
                "rating": rating_int,
                "supporter": self.supporter_input.value,
                "timestamp": datetime.now(UTC).isoformat()
            })
            save_data("vouch_data.json", vouch_data)

            stars = "⭐" * rating_int
            embed = create_embed(
                f"🏆 Vouch from {interaction.user}",
                f"Total vouches: {vouch_data[user_id]['count']}",
                discord.Color.purple(),
                thumbnail=interaction.user.display_avatar.url
            )
            embed.add_field(name="Product", value=f"`{self.product_input.value}`", inline=True)
            embed.add_field(name="Star Rating", value=stars, inline=True)
            if self.supporter_input.value:
                embed.add_field(name="Supporter", value=self.supporter_input.value, inline=False)
            embed.add_field(name="Experience", value=f"```{self.experience_input.value}```", inline=False)
            
            vouch_channel = discord.utils.get(interaction.guild.text_channels, name=VOUCH_CHANNEL_NAME)
            if vouch_channel:
                await vouch_channel.send(embed=embed)
                await interaction.response.send_message("✅ Your vouch has been submitted!", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Vouch channel not found. Vouch submitted internally but not posted.", ephemeral=True)

        except ValueError:
            await interaction.response.send_message("❌ Invalid rating. Please enter a number between 1 and 5.", ephemeral=True)

class VouchPanelView(discord.ui.View):
    @discord.ui.button(label="Submit Vouch", style=discord.ButtonStyle.green, emoji="🏆")
    async def submit_vouch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VouchModal())

class DeliverKeyButtonView(discord.ui.View):
    def __init__(self, user, product):
        super().__init__()
        self.user = user
        self.product = product

    @discord.ui.button(label="Deliver Key", style=discord.ButtonStyle.green)
    async def deliver(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You must be an admin to use this button.", ephemeral=True)
            return
        if not SHEETS_ENABLED:
            await interaction.response.send_message("❌ Google Sheets functionality is not available.", ephemeral=True)
            return
        key = get_key(self.product, f"{self.user.name}#{self.user.discriminator}")
        if key:
            try:
                await self.user.send(
                    f"✅ Thanks for your purchase of **{self.product}**!\nHere is your license key:\n`{key}`"
                )
                customer_role = discord.utils.get(interaction.guild.roles, name="Customer")
                buyer_role = discord.utils.get(interaction.guild.roles, name="Buyer")
                member = interaction.guild.get_member(self.user.id)
                if customer_role and member:
                    await member.add_roles(customer_role, reason="Purchase confirmed")
                if buyer_role and member and buyer_role in member.roles:
                    await member.remove_roles(buyer_role, reason="Promoted to Customer")
                embed = create_embed("Key Delivered", f"Key for **{self.product}** sent to {self.user.mention}", discord.Color.green())
                await interaction.response.send_message(embed=embed, ephemeral=True, view=PostPurchaseVouchView(self.user, self.product))
                await log_to_channel(interaction.guild, f"✅ Key manually delivered to `{self.user}` | Product: **{self.product}**", LOG_CHANNEL_NAME)
            except discord.Forbidden:
                await interaction.response.send_message("❌ Failed to DM the user. They may have DMs off.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ No available keys for **{self.product}**.", ephemeral=True)

class PostPurchaseVouchView(discord.ui.View):
    def __init__(self, user, product):
        super().__init__(timeout=None)
        self.user = user
        self.product = product
        
    @discord.ui.button(label="Leave a Vouch", style=discord.ButtonStyle.blurple, emoji="✍️")
    async def leave_vouch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ This button is for the customer who received the key.", ephemeral=True)
            return
        await interaction.response.send_modal(VouchModal(prefilled_product=self.product))

class TicketDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Buy", emoji="💳"),
            discord.SelectOption(label="Exchange", emoji="🔄"),
            discord.SelectOption(label="Support", emoji="💬"),
            discord.SelectOption(label="Reseller Apply", emoji="🤝"),
            discord.SelectOption(label="Media", emoji="🖼️"),
            discord.SelectOption(label="Giveaway", emoji="🎁"),
        ]
        super().__init__(placeholder="Select ticket reason...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        existing = discord.utils.get(guild.text_channels, name=f"ticket-{interaction.user.name}-{interaction.user.discriminator}".lower())
        if existing:
            await interaction.response.send_message("❗ You already have an open ticket.", ephemeral=True)
            return
        category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
        if not category:
            category = await guild.create_category(TICKET_CATEGORY_NAME)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True)
        }
        try:
            name = f"ticket-{interaction.user.name}-{interaction.user.discriminator}".replace(" ", "-").lower()
            channel = await guild.create_text_channel(name=name, category=category, overwrites=overwrites)
        except discord.HTTPException:
            await interaction.response.send_message("❌ Failed to create ticket channel. Please try again later.", ephemeral=True)
            return
        await channel.send(
            f"🎛 Ticket created by {interaction.user.mention} for **{self.values[0]}**",
            view=CloseButtonView()
        )
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)

class CloseButtonView(discord.ui.View):
    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        messages = []
        async for message in interaction.channel.history(limit=100, oldest_first=True):
            messages.append(f"[{message.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {message.author}: {message.content}")
        transcript_text = "\n".join(messages)
        transcript_file = discord.File(io.BytesIO(transcript_text.encode()), filename=f"transcript-{interaction.channel.name}.txt")
        await log_to_channel(interaction.guild, f"📝 Transcript for `{interaction.channel.name}` (closed by {interaction.user}):", TRANSCRIPT_CHANNEL_NAME)
        log_channel = await find_or_create_channel(interaction.guild, TRANSCRIPT_CHANNEL_NAME)
        await log_channel.send(file=transcript_file)
        await interaction.response.send_message("❌ Ticket closed and transcript saved.", ephemeral=True)
        await interaction.channel.delete()

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(TicketDropdown())

class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="Enter Giveaway", style=discord.ButtonStyle.green, custom_id="giveaway_entry")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.guild.id) not in giveaways_data or self.giveaway_id not in giveaways_data[str(interaction.guild.id)]:
            await interaction.response.send_message("❌ This giveaway is no longer active.", ephemeral=True)
            return
        
        giveaway = giveaways_data[str(interaction.guild.id)][self.giveaway_id]
        if interaction.user.id in giveaway["entries"]:
            await interaction.response.send_message("❗ You have already entered this giveaway.", ephemeral=True)
        else:
            giveaway["entries"].append(interaction.user.id)
            save_data("giveaways.json", giveaways_data)
            await interaction.response.send_message("✅ You have entered the giveaway!", ephemeral=True)

class ReactionRoleView(discord.ui.View):
    def __init__(self, roles_map):
        super().__init__(timeout=None)
        self.roles_map = roles_map
        for emoji, role_id in roles_map.items():
            self.add_item(ReactionRoleButton(emoji, role_id))

class ReactionRoleButton(discord.ui.Button):
    def __init__(self, emoji, role_id):
        super().__init__(emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=f"reaction_role_{role_id}")
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if not role:
            await interaction.response.send_message("❌ The role for this reaction no longer exists.", ephemeral=True)
            return

        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            await interaction.response.send_message(f"✅ Removed the `{role.name}` role.", ephemeral=True)
        else:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(f"✅ You now have the `{role.name}` role.", ephemeral=True)


# --- EVENTS ---
@bot.event
async def on_ready():
    # Start the web server in a separate thread
    web_server_thread = threading.Thread(target=web_server, daemon=True)
    web_server_thread.start()

    print(f"✅ Logged in as {bot.user}")
    print(f"📊 Bot is in {len(bot.guilds)} guilds")
    
    if SHEETS_ENABLED and not validate_sheet_columns():
        print("❌ Sheet validation failed. Please check column headers.")
        print("⚠️  Google Sheets commands may not work properly.")
    
    try:
        synced = await bot.tree.sync()
        print(f"🔁 Synced {len(synced)} commands globally.")
        for command in synced:
            print(f"   - /{command.name}")
    except Exception as e:
        print(f"❌ Global sync error: {e}")
        print(f"❌ Full error: {traceback.format_exc()}")
        
    print("🏠 Bot is active in:")
    for guild in bot.guilds:
        print(f"   - {guild.name} (ID: {guild.id})")
        
    check_temp_bans.start()
    backup_data_task.start()
    check_giveaways.start()

@bot.event
async def on_guild_join(guild):
    print(f"🎉 Bot joined new guild: {guild.name} (ID: {guild.id})")
    try:
        synced = await bot.tree.sync()
        print(f"🔁 Re-synced {len(synced)} commands after joining {guild.name}")
    except Exception as e:
        print(f"❌ Sync error after joining {guild.name}: {e}")

@bot.event
async def on_guild_remove(guild):
    print(f"👋 Bot left guild: {guild.name} (ID: {guild.id})")
    if str(guild.id) in warnings_data:
        del warnings_data[str(guild.id)]
        save_data("warnings.json", warnings_data)
    if str(guild.id) in auto_roles_data:
        del auto_roles_data[str(guild.id)]
        save_data("auto_roles.json", auto_roles_data)
    if str(guild.id) in giveaways_data:
        del giveaways_data[str(guild.id)]
        save_data("giveaways.json", giveaways_data)

@bot.event
async def on_member_join(member):
    guild = member.guild
    if str(guild.id) in auto_roles_data:
        for role_id in auto_roles_data[str(guild.id)]:
            role = guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role)
                except discord.Forbidden:
                    print(f"❌ Missing permissions to add role '{role.name}' to {member.name}. Check bot role hierarchy.")
    welcome_channel = discord.utils.get(guild.text_channels, name=WELCOME_CHANNEL_NAME)
    if welcome_channel:
        embed = create_embed(
            f"🎉 Welcome to {guild.name}!",
            f"Hello {member.mention}! We're happy to have you here. There are now {guild.member_count} members!",
            discord.Color.green(),
            thumbnail=member.display_avatar.url
        )
        await welcome_channel.send(embed=embed)
    if guild.id in bot.stats_channels:
        await update_stats_channels(guild)

@bot.event
async def on_member_remove(member):
    guild = member.guild
    if guild.id in bot.stats_channels:
        await update_stats_channels(guild)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.author.id in afk_data:
        afk_reason = afk_data.pop(message.author.id)
        save_data("afk_status.json", afk_data)
        await message.channel.send(f"✅ Welcome back, {message.author.mention}! I've removed your AFK status. You were AFK for: **{afk_reason}**", delete_after=10)

    for member in message.mentions:
        if member.id in afk_data:
            afk_reason = afk_data[member.id]
            embed = create_embed(
                f"💤 {member.name} is AFK",
                f"Reason: `{afk_reason}`",
                discord.Color.yellow()
            )
            await message.channel.send(embed=embed, delete_after=15)
            
    await bot.process_commands(message)

# --- GLOBAL ERROR HANDLER ---
@bot.event
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    error_msg = f"❌ An error occurred: {str(error)}"
    print(f"Command error in {interaction.guild.name if interaction.guild else 'DM'}: {error}")
    print(f"Full traceback: {traceback.format_exc()}")
    
    try:
        if isinstance(error, app_commands.MissingPermissions):
            error_msg = f"❌ You do not have the required permissions to use this command: `{'`, `'.join(error.missing_permissions)}`."
        elif isinstance(error, app_commands.BotMissingPermissions):
            error_msg = f"❌ I do not have the required permissions to do that: `{'`, `'.join(error.missing_permissions)}`."
        elif isinstance(error, app_commands.CommandOnCooldown):
            error_msg = f"⏳ This command is on cooldown. Try again in {error.retry_after:.2f} seconds."

        if not interaction.response.is_done():
            await interaction.response.send_message(error_msg, ephemeral=True)
        else:
            await interaction.followup.send(error_msg, ephemeral=True)
    except:
        pass

# --- TICKET & PRODUCT KEY COMMANDS ---
@bot.tree.command(name="ticket", description="Open the ticket panel")
async def ticket_panel(interaction: discord.Interaction):
    embed = create_embed("📨 Tickets", "Create a support ticket by selecting an option below.", discord.Color.blue())
    await interaction.response.send_message(embed=embed, view=TicketView())

@bot.tree.command(name="payment", description="Show payment options in Euros")
async def payment_menu(interaction: discord.Interaction):
    embed = create_embed("💳 Payment Methods", "Accepted payment methods:", discord.Color.green())
    embed.add_field(name="PayPal (F&F)", value="paypal@example.com", inline=False)
    embed.add_field(name="UPI", value="northselling@upi", inline=False)
    embed.add_field(name="Paysafecard", value="DM a staff member for PSC code instructions", inline=False)
    embed.add_field(name="Crypto", value="BTC: `1ExampleBTC`\nETH: `0xExampleETH`\nLTC: `LExampleLTC`", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="confirm_payment", description="Owner-only: Confirm payment and deliver key")
@app_commands.describe(user="User to deliver the key to", product="Name of the product")
async def confirm_payment(interaction: discord.Interaction, user: discord.User, product: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You must be an admin to use this command.", ephemeral=True)
        return
    view = DeliverKeyButtonView(user, product)
    await interaction.response.send_message(f"✅ Use the button below to deliver a key to {user.mention} for **{product}**.", view=view, ephemeral=True)

@bot.tree.command(name="check_keys", description="Admin-only: Check product stock")
async def check_keys(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You must be an admin to use this command.", ephemeral=True)
        return
    if not SHEETS_ENABLED:
        await interaction.response.send_message("❌ Google Sheets functionality is not available.", ephemeral=True)
        return
    summary = get_stock_summary()
    embed = create_embed("📦 Product Key Stock", None, discord.Color.orange())
    if "Error" in summary:
        embed.add_field(name="Error", value=summary["Error"], inline=False)
    else:
        if not summary:
            embed.add_field(name="No Products", value="No products found in the spreadsheet.", inline=False)
        else:
            for product, count in summary.items():
                embed.add_field(name=product, value=f"{count} key(s)", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="add_keys", description="Admin-only: Add new license keys")
@app_commands.describe(product="Product name", keys="Comma-separated list of keys")
async def add_keys(interaction: discord.Interaction, product: str, keys: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You must be an admin to use this command.", ephemeral=True)
        return
    if not SHEETS_ENABLED:
        await interaction.response.send_message("❌ Google Sheets functionality is not available.", ephemeral=True)
        return
    key_list = [key.strip() for key in keys.split(",") if key.strip()]
    if not key_list:
        await interaction.response.send_message("❌ No valid keys provided.", ephemeral=True)
        return
    try:
        headers = sheet.row_values(1)
        product_col = headers.index("Product") + 1
        key_col = headers.index("Key") + 1
        used_col = headers.index("Used") + 1
        user_col = headers.index("User") + 1
        next_row = len(sheet.get_all_values()) + 1
        for key in key_list:
            sheet.update(f"A{next_row}:D{next_row}", [[product, key, "No", ""]])
            next_row += 1
        await interaction.response.send_message(f"✅ Added {len(key_list)} key(s) for **{product}**.", ephemeral=True)
        await log_to_channel(interaction.guild, f"➕ `{interaction.user}` added {len(key_list)} key(s) for **{product}**", LOG_CHANNEL_NAME)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error adding keys: {e}", ephemeral=True)

# --- PRODUCT EMBED COMMANDS ---
@bot.tree.command(name="product_embed_template", description="Post a saved template product embed")
@app_commands.describe(template="Name of the saved template", target_channel="Channel to post the embed")
async def product_embed_template(interaction: discord.Interaction, template: str, target_channel: discord.TextChannel):
    if not templates_data or template not in templates_data:
        await interaction.response.send_message("❌ Template not found.", ephemeral=True)
        return
    data = templates_data[template]

    class ProductEmbedButton(discord.ui.View):
        def __init__(self, ticket_reason):
            super().__init__()
            self.ticket_reason = ticket_reason
        @discord.ui.button(label="More Info", style=discord.ButtonStyle.primary)
        async def info(self, interaction2: discord.Interaction, button: discord.ui.Button):
            guild = interaction2.guild
            existing = discord.utils.get(guild.text_channels, name=f"ticket-{interaction2.user.name}-{interaction2.user.discriminator}".lower())
            if existing:
                await interaction2.response.send_message("❗ You already have an open ticket.", ephemeral=True)
                return
            category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
            if not category:
                category = await guild.create_category(TICKET_CATEGORY_NAME)
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction2.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                guild.me: discord.PermissionOverwrite(view_channel=True)
            }
            name = f"ticket-{interaction2.user.name}-{interaction2.user.discriminator}".replace(" ", "-").lower()
            channel = await guild.create_text_channel(name=name, category=category, overwrites=overwrites)
            await channel.send(
                f"🎛 Ticket created by {interaction2.user.mention} for **{self.ticket_reason}**",
                view=CloseButtonView()
            )
            await interaction2.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)

    embed = create_embed(data["title"], data["description"], discord.Color.blurple())
    embed.set_image(url=data["image_url"])
    await target_channel.send(embed=embed, view=ProductEmbedButton(data["ticket_reason"]))
    await interaction.response.send_message(f"✅ Template embed sent to {target_channel.mention}.", ephemeral=True)

@bot.tree.command(name="save_template", description="Save a new product embed template")
@app_commands.describe(name="Template name", title="Title", description="Description", image_url="Image URL", ticket_reason="Ticket dropdown label")
async def save_template(interaction: discord.Interaction, name: str, title: str, description: str, image_url: str, ticket_reason: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    templates_data[name] = {
        "title": title,
        "description": description,
        "image_url": image_url,
        "ticket_reason": ticket_reason
    }
    try:
        save_data("templates.json", templates_data)
        await interaction.response.send_message(f"✅ Template `{name}` saved.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error saving template: {e}", ephemeral=True)

@bot.tree.command(name="edit_template", description="Edit an existing product embed template")
@app_commands.describe(name="Template name to edit", field="Field to update", value="New value")
async def edit_template(interaction: discord.Interaction, name: str, field: str, value: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    if name not in templates_data or field not in templates_data[name]:
        await interaction.response.send_message("❌ Template or field not found.", ephemeral=True)
        return
    templates_data[name][field] = value
    try:
        save_data("templates.json", templates_data)
        await interaction.response.send_message(f"✏️ Updated `{field}` for template `{name}`.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error updating template: {e}", ephemeral=True)

@bot.tree.command(name="list_templates", description="List all available product embed templates")
async def list_templates(interaction: discord.Interaction):
    if not templates_data:
        await interaction.response.send_message("❌ No templates found.", ephemeral=True)
        return
    embed = create_embed("🧩 Available Embed Templates", "Use `/product_embed_template` with one of these:", discord.Color.teal())
    for name, data in templates_data.items():
        embed.add_field(name=name, value=data.get("title", "No title"), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="preview_template", description="Preview a saved product template without posting")
@app_commands.describe(name="Template name to preview")
async def preview_template(interaction: discord.Interaction, name: str):
    if not templates_data or name not in templates_data:
        await interaction.response.send_message("❌ Template not found.", ephemeral=True)
        return
    t = templates_data[name]
    embed = create_embed(t["title"], t["description"], discord.Color.blurple())
    embed.set_image(url=t["image_url"])
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="product_embed", description="Post a product embed to any channel with a ticket button")
@app_commands.describe(
    title="Product title",
    description="Product description", 
    image_url="URL of the image/banner",
    ticket_reason="Ticket dropdown label to prefill",
    target_channel="Channel to post the embed"
)
async def product_embed(interaction: discord.Interaction, title: str, description: str, image_url: str, ticket_reason: str, target_channel: discord.TextChannel):
    class ProductEmbedButton(discord.ui.View):
        def __init__(self, reason):
            super().__init__()
            self.reason = reason
        @discord.ui.button(label="More Info", style=discord.ButtonStyle.primary)
        async def info(self, interaction2: discord.Interaction, button: discord.ui.Button):
            await interaction2.response.send_message(f"🎫 Creating ticket for **{self.reason}**...", ephemeral=True)

    embed = create_embed(title, description, discord.Color.blurple())
    embed.set_image(url=image_url)
    await target_channel.send(embed=embed, view=ProductEmbedButton(ticket_reason))
    await interaction.response.send_message(f"✅ Product embed sent to {target_channel.mention}.", ephemeral=True)

# --- REDESIGNED VOUCH SYSTEM COMMANDS ---
@bot.tree.command(name="vouch", description="Submit a vouch for a product or service")
async def vouch(interaction: discord.Interaction):
    await interaction.response.send_modal(VouchModal())

@bot.tree.command(name="reputation", description="Check a user's vouch count and history")
@app_commands.describe(user="The user to check reputation for (defaults to yourself)")
async def reputation(interaction: discord.Interaction, user: discord.Member = None):
    member = user or interaction.user
    user_id = str(member.id)
    if user_id not in vouch_data or vouch_data[user_id]["count"] == 0:
        await interaction.response.send_message(f"❌ {member.mention} has no vouches yet.", ephemeral=True)
        return

    vouch_count = vouch_data[user_id]["count"]
    vouches = vouch_data[user_id]["vouches"]
    
    embed = create_embed(
        f"🏆 Vouch Reputation for {member.name}",
        f"Total vouches: **{vouch_count}**",
        discord.Color.purple(),
        thumbnail=member.display_avatar.url
    )
    
    for i, vouch_entry in enumerate(vouches[-5:], 1):
        stars = "⭐" * vouch_entry["rating"]
        supporter_text = f"\nSupporter: {vouch_entry['supporter']}" if vouch_entry['supporter'] else ""
        embed.add_field(
            name=f"Vouch #{vouch_count - len(vouches) + i} for {vouch_entry['product']} {stars}",
            value=f"```{vouch_entry['experience']}``` {supporter_text}\nDate: <t:{int(datetime.fromisoformat(vouch_entry['timestamp']).timestamp())}:R>",
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setup_vouch_panel", description="Post a persistent vouch panel with a button")
@app_commands.checks.has_permissions(manage_channels=True)
async def setup_vouch_panel(interaction: discord.Interaction):
    embed = create_embed(
        "🏆 Leave a Vouch!",
        "Click the button below to submit your experience and support for our products and service.",
        discord.Color.green()
    )
    await interaction.channel.send(embed=embed, view=VouchPanelView())
    await interaction.response.send_message(f"✅ Vouch panel has been set up in {interaction.channel.mention}!", ephemeral=True)


# --- MODERATION COMMANDS ---
@bot.tree.command(name="warn", description="Warn a user with a reason")
@app_commands.describe(user="The user to warn", reason="The reason for the warning")
@app_commands.checks.has_permissions(kick_members=True)
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    guild_id = str(interaction.guild.id)
    user_id = str(user.id)
    if guild_id not in warnings_data:
        warnings_data[guild_id] = {}
    if user_id not in warnings_data[guild_id]:
        warnings_data[guild_id][user_id] = []
    
    warnings_data[guild_id][user_id].append({"reason": reason, "moderator": str(interaction.user), "timestamp": datetime.now(UTC).isoformat()})
    save_data("warnings.json", warnings_data)
    
    embed = create_embed("⚠️ User Warned", f"{user.mention} has been warned.", discord.Color.orange())
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Total Warnings", value=len(warnings_data[guild_id][user_id]), inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="warnings", description="View a user's warning history")
@app_commands.describe(user="The user to check warnings for")
@app_commands.checks.has_permissions(kick_members=True)
async def warnings(interaction: discord.Interaction, user: discord.Member):
    guild_id = str(interaction.guild.id)
    user_id = str(user.id)
    if guild_id not in warnings_data or user_id not in warnings_data[guild_id] or not warnings_data[guild_id][user_id]:
        await interaction.response.send_message("✅ This user has no warnings.", ephemeral=True)
        return
        
    embed = create_embed(f"⚠️ Warnings for {user.name}", None, discord.Color.red(), thumbnail=user.display_avatar.url)
    for i, warning in enumerate(warnings_data[guild_id][user_id], 1):
        embed.add_field(name=f"Warning {i} by {warning['moderator']}", value=f"Reason: `{warning['reason']}`\nDate: <t:{int(datetime.fromisoformat(warning['timestamp']).timestamp())}:R>", inline=False)
        
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clear_warnings", description="Clear a user's warning history")
@app_commands.describe(user="The user to clear warnings for")
@app_commands.checks.has_permissions(kick_members=True)
async def clear_warnings(interaction: discord.Interaction, user: discord.Member):
    guild_id = str(interaction.guild.id)
    user_id = str(user.id)
    
    if guild_id in warnings_data and user_id in warnings_data[guild_id]:
        warnings_data[guild_id][user_id] = []
        save_data("warnings.json", warnings_data)
        await interaction.response.send_message(f"✅ Cleared all warnings for {user.mention}.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ This user has no warnings to clear.", ephemeral=True)

@bot.tree.command(name="tempban", description="Temporarily ban a user")
@app_commands.describe(user="The user to tempban", duration="Duration (e.g., 1h, 2d, 1w)", reason="Reason for the ban")
@app_commands.checks.has_permissions(ban_members=True)
async def tempban(interaction: discord.Interaction, user: discord.Member, duration: str, reason: str):
    unit = duration[-1]
    value = int(duration[:-1])
    delta = None
    if unit == 's':
        delta = timedelta(seconds=value)
    elif unit == 'm':
        delta = timedelta(minutes=value)
    elif unit == 'h':
        delta = timedelta(hours=value)
    elif unit == 'd':
        delta = timedelta(days=value)
    elif unit == 'w':
        delta = timedelta(weeks=value)
    
    if not delta:
        await interaction.response.send_message("❌ Invalid duration format. Use `10m`, `2h`, `7d`, etc.", ephemeral=True)
        return
        
    end_time = datetime.now(UTC) + delta
    ban_reason = f"Tempban until: {end_time.isoformat()} | Reason: {reason} | Moderator: {interaction.user}"
    await user.ban(reason=ban_reason)
    
    embed = create_embed("🔨 Tempban Issued", f"{user.mention} has been temporarily banned.", discord.Color.red())
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Expires", value=f"<t:{int(end_time.timestamp())}:F>", inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="mute", description="Timeout a user for a specified duration")
@app_commands.describe(user="The user to timeout", duration="Duration (e.g., 10m, 2h, 1d)", reason="Reason for the timeout")
@app_commands.checks.has_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, user: discord.Member, duration: str, reason: str):
    unit = duration[-1]
    value = int(duration[:-1])
    delta = None
    if unit == 's':
        delta = timedelta(seconds=value)
    elif unit == 'm':
        delta = timedelta(minutes=value)
    elif unit == 'h':
        delta = timedelta(hours=value)
    elif unit == 'd':
        delta = timedelta(days=value)
    
    if not delta:
        await interaction.response.send_message("❌ Invalid duration format. Use `10m`, `2h`, `1d`, etc.", ephemeral=True)
        return
        
    await user.timeout(delta, reason=reason)
    
    embed = create_embed("🔇 User Timed Out", f"{user.mention} has been timed out.", discord.Color.yellow())
    embed.add_field(name="Duration", value=duration, inline=True)
    embed.add_field(name="Reason", value=reason, inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="unmute", description="Remove a user's timeout")
@app_commands.describe(user="The user to remove the timeout from")
@app_commands.checks.has_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, user: discord.Member):
    if user.is_timed_out():
        await user.timeout(None)
        await interaction.response.send_message(f"✅ Timeout removed for {user.mention}.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ This user is not currently timed out.", ephemeral=True)

@bot.tree.command(name="clear_channel", description="Bulk delete messages from a channel")
@app_commands.describe(amount="Number of messages to delete (max 100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear_channel(interaction: discord.Interaction, amount: int):
    if amount > 100 or amount < 1:
        await interaction.response.send_message("❌ You can only delete between 1 and 100 messages.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"✅ Deleted {len(deleted)} message(s).", ephemeral=True)

# --- GIVEAWAY SYSTEM ---
@bot.tree.command(name="giveaway", description="Start a new giveaway")
@app_commands.describe(duration="Duration (e.g., 1h, 2d)", winner_count="Number of winners", prize="The prize for the giveaway")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway(interaction: discord.Interaction, duration: str, winner_count: int, prize: str):
    
    delta = parse_duration(duration)
    if not delta:
        await interaction.response.send_message("❌ Invalid duration format. Use `1d 5h 30m`, `2h`, etc.", ephemeral=True)
        return
    
    if winner_count < 1:
        await interaction.response.send_message("❌ Winner count must be at least 1.", ephemeral=True)
        return

    end_time = datetime.now(UTC) + delta
    
    embed = create_embed(
        "🎁 GIVEAWAY! 🎁",
        f"**Prize**: {prize}\n**Winners**: {winner_count}\n**Ends**: <t:{int(end_time.timestamp())}:R>",
        discord.Color.gold(),
        fields=[("Hosted By", interaction.user.mention, True)]
    )
    
    view = GiveawayView(f"{interaction.guild.id}-{interaction.channel.id}-{interaction.id}")
    giveaway_channel = discord.utils.get(interaction.guild.text_channels, name=GIVEAWAY_CHANNEL_NAME)
    if not giveaway_channel:
        await interaction.response.send_message(f"❌ Giveaway channel '{GIVEAWAY_CHANNEL_NAME}' not found. Please create it first.", ephemeral=True)
        return

    message = await giveaway_channel.send(embed=embed, view=view)
    
    giveaway_info = {
        "channel_id": giveaway_channel.id,
        "message_id": message.id,
        "prize": prize,
        "winner_count": winner_count,
        "end_time": end_time.isoformat(),
        "entries": [],
        "host": interaction.user.id
    }
    
    guild_id_str = str(interaction.guild.id)
    if guild_id_str not in giveaways_data:
        giveaways_data[guild_id_str] = {}
        
    giveaways_data[guild_id_str][str(message.id)] = giveaway_info
    save_data("giveaways.json", giveaways_data)
    
    await interaction.response.send_message(f"✅ Giveaway started in {giveaway_channel.mention}!", ephemeral=True)
    await log_to_channel(interaction.guild, f"🎉 `{interaction.user}` started a giveaway for **{prize}** in {giveaway_channel.mention}.", GIVEAWAY_LOGS_CHANNEL_NAME)

@bot.tree.command(name="giveaway_end", description="End a giveaway early")
@app_commands.describe(message_id="The ID of the giveaway message")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_end(interaction: discord.Interaction, message_id: str):
    guild_id_str = str(interaction.guild.id)
    if guild_id_str not in giveaways_data or message_id not in giveaways_data[guild_id_str]:
        await interaction.response.send_message("❌ Giveaway not found or has already ended.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    giveaway_info = giveaways_data[guild_id_str][message_id]
    await end_giveaway_logic(interaction.guild, giveaway_info)
    await interaction.followup.send("✅ Giveaway has been ended.", ephemeral=True)
    await log_to_channel(interaction.guild, f"🛑 `{interaction.user}` manually ended the giveaway for **{giveaway_info['prize']}**.", GIVEAWAY_LOGS_CHANNEL_NAME)

@bot.tree.command(name="giveaway_reroll", description="Reroll a winner for a past giveaway")
@app_commands.describe(message_id="The ID of the past giveaway message")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_reroll(interaction: discord.Interaction, message_id: str):
    await interaction.response.defer(ephemeral=True)
    
    found_giveaway = None
    for guild_id_str, giveaways in giveaways_data.items():
        if message_id in giveaways:
            found_giveaway = giveaways[message_id]
            break
            
    if not found_giveaway:
        await interaction.followup.send("❌ Giveaway not found or is still active.", ephemeral=True)
        return
    
    entries = found_giveaway["entries"]
    if not entries:
        await interaction.followup.send("❌ No entries found for this giveaway.", ephemeral=True)
        return
        
    new_winner = random.choice(entries)
    new_winner_mention = f"<@{new_winner}>"
    
    reroll_embed = create_embed(
        "🎉 Giveaway Reroll!",
        f"A new winner has been selected for the giveaway for **{found_giveaway['prize']}**.",
        discord.Color.green()
    )
    reroll_embed.add_field(name="New Winner", value=new_winner_mention, inline=False)
    
    giveaway_channel = bot.get_channel(found_giveaway['channel_id'])
    await giveaway_channel.send(content=f"🎉 Congratulations, {new_winner_mention}!", embed=reroll_embed)
    await interaction.followup.send("✅ Winner has been rerolled and announced.", ephemeral=True)
    await log_to_channel(interaction.guild, f"🔁 `{interaction.user}` rerolled the giveaway for **{found_giveaway['prize']}**. The new winner is {new_winner_mention}.", GIVEAWAY_LOGS_CHANNEL_NAME)


# --- AUTO-MOD & ROLES ---
@bot.tree.command(name="setup_reaction_role", description="Set up a reaction role message")
@app_commands.describe(channel="Channel to post the message in", title="Embed title", description="Embed description", role1="Role 1", emoji1="Emoji 1")
@app_commands.checks.has_permissions(manage_roles=True)
async def setup_reaction_role(interaction: discord.Interaction, channel: discord.TextChannel, title: str, description: str, role1: discord.Role, emoji1: str, role2: discord.Role = None, emoji2: str = None, role3: discord.Role = None, emoji3: str = None):
    roles_map = {}
    if role1 and emoji1:
        roles_map[emoji1] = role1.id
    if role2 and emoji2:
        roles_map[emoji2] = role2.id
    if role3 and emoji3:
        roles_map[emoji3] = role3.id
    
    if not roles_map:
        await interaction.response.send_message("❌ Please provide at least one role and emoji.", ephemeral=True)
        return

    embed = create_embed(title, description, discord.Color.blurple())
    view = ReactionRoleView(roles_map)
    await channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Reaction role message sent!", ephemeral=True)

@bot.tree.command(name="add_auto_role", description="Add a role that is automatically assigned to new members")
@app_commands.describe(role="The role to auto-assign")
@app_commands.checks.has_permissions(manage_roles=True)
async def add_auto_role(interaction: discord.Interaction, role: discord.Role):
    guild_id = str(interaction.guild.id)
    if guild_id not in auto_roles_data:
        auto_roles_data[guild_id] = []
    
    if role.id in auto_roles_data[guild_id]:
        await interaction.response.send_message("❌ This role is already an auto-role.", ephemeral=True)
        return
        
    auto_roles_data[guild_id].append(role.id)
    save_data("auto_roles.json", auto_roles_data)
    await interaction.response.send_message(f"✅ Added `{role.name}` to the list of auto-roles.", ephemeral=True)

@bot.tree.command(name="remove_auto_role", description="Remove a role from the auto-assign list")
@app_commands.describe(role="The role to remove")
@app_commands.checks.has_permissions(manage_roles=True)
async def remove_auto_role(interaction: discord.Interaction, role: discord.Role):
    guild_id = str(interaction.guild.id)
    if guild_id in auto_roles_data and role.id in auto_roles_data[guild_id]:
        auto_roles_data[guild_id].remove(role.id)
        save_data("auto_roles.json", auto_roles_data)
        await interaction.response.send_message(f"✅ Removed `{role.name}` from the auto-role list.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ That role is not currently an auto-role.", ephemeral=True)

@bot.tree.command(name="list_auto_roles", description="List all roles that are automatically assigned")
@app_commands.checks.has_permissions(manage_roles=True)
async def list_auto_roles(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    if guild_id not in auto_roles_data or not auto_roles_data[guild_id]:
        await interaction.response.send_message("❌ No auto-roles are set up for this server.", ephemeral=True)
        return
        
    roles = [f"- {interaction.guild.get_role(role_id).name}" for role_id in auto_roles_data[guild_id] if interaction.guild.get_role(role_id)]
    
    embed = create_embed(
        "🤖 Auto-Roles",
        "\n".join(roles) if roles else "No auto-roles configured.",
        discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="afk", description="Set your AFK status")
@app_commands.describe(reason="The reason you are AFK")
async def afk(interaction: discord.Interaction, reason: str = "I am AFK."):
    afk_data[interaction.user.id] = reason
    save_data("afk_status.json", afk_data)
    await interaction.response.send_message(f"✅ You are now AFK. Reason: `{reason}`", ephemeral=True)

# --- SERVER MANAGEMENT & UTILITIES ---
@bot.tree.command(name="userinfo", description="Get information about a user")
@app_commands.describe(user="The user to get information about (optional)")
async def userinfo(interaction: discord.Interaction, user: discord.Member = None):
    member = user or interaction.user
    embed = create_embed(f"User Info: {member.name}", None, discord.Color.blue(), thumbnail=member.display_avatar.url)
    embed.add_field(name="Username", value=member.name, inline=True)
    embed.add_field(name="Discriminator", value=member.discriminator, inline=True)
    embed.add_field(name="ID", value=member.id, inline=False)
    embed.add_field(name="Joined Server", value=f"<t:{int(member.joined_at.timestamp())}:F>", inline=False)
    embed.add_field(name="Joined Discord", value=f"<t:{int(member.created_at.timestamp())}:F>", inline=False)
    
    roles = [role.mention for role in member.roles if role.name != "@everyone"]
    if roles:
        embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles), inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="serverinfo", description="Get information about the server")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    embed = create_embed(f"Server Info: {guild.name}", None, discord.Color.blurple(), thumbnail=guild.icon.url if guild.icon else None)
    embed.add_field(name="Owner", value=guild.owner.mention, inline=True)
    embed.add_field(name="Server ID", value=guild.id, inline=True)
    embed.add_field(name="Members", value=guild.member_count, inline=True)
    embed.add_field(name="Bots", value=len([m for m in guild.members if m.bot]), inline=True)
    embed.add_field(name="Channels", value=len(guild.channels), inline=True)
    embed.add_field(name="Roles", value=len(guild.roles), inline=True)
    embed.add_field(name="Created On", value=f"<t:{int(guild.created_at.timestamp())}:F>", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="poll", description="Create a poll with up to 10 options")
@app_commands.describe(question="The poll question", option1="Option 1", option2="Option 2")
async def poll(interaction: discord.Interaction, question: str, option1: str, option2: str, option3: str = None, option4: str = None, option5: str = None):
    options = [opt for opt in [option1, option2, option3, option4, option5] if opt is not None]
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    
    poll_text = "\n".join([f"{emojis[i]} {opt}" for i, opt in enumerate(options)])
    
    embed = create_embed(f"📊 Poll: {question}", poll_text, discord.Color.purple())
    message = await interaction.channel.send(embed=embed)
    
    for i in range(len(options)):
        await message.add_reaction(emojis[i])
        
    await interaction.response.send_message("✅ Poll created!", ephemeral=True)

@bot.tree.command(name="announce", description="Make a professional announcement")
@app_commands.describe(channel="Channel to post in", title="Announcement title", message="The announcement message")
@app_commands.checks.has_permissions(mention_everyone=True)
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, title: str, message: str):
    embed = create_embed(f"📢 {title}", message, discord.Color.gold())
    await channel.send(embed=embed)
    await interaction.response.send_message("✅ Announcement sent!", ephemeral=True)

async def update_stats_channels(guild):
    member_count_channel = discord.utils.get(guild.voice_channels, name=MEMBER_COUNT_CHANNEL_NAME.format(count=guild.member_count))
    bot_count_channel = discord.utils.get(guild.voice_channels, name=BOT_COUNT_CHANNEL_NAME.format(count=len([m for m in guild.members if m.bot])))

    if member_count_channel:
        try:
            new_name = MEMBER_COUNT_CHANNEL_NAME.format(count=guild.member_count)
            await member_count_channel.edit(name=new_name)
        except discord.Forbidden:
            print(f"❌ Missing permissions to edit channel in {guild.name}")
    
    if bot_count_channel:
        try:
            new_name = BOT_COUNT_CHANNEL_NAME.format(count=len([m for m in guild.members if m.bot]))
            await bot_count_channel.edit(name=new_name)
        except discord.Forbidden:
            print(f"❌ Missing permissions to edit channel in {guild.name}")

@bot.tree.command(name="setup_stats", description="Setup auto-updating member and bot count channels")
@app_commands.checks.has_permissions(manage_channels=True)
async def setup_stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    category = discord.utils.get(interaction.guild.categories, name=STATS_CATEGORY_NAME)
    if not category:
        category = await interaction.guild.create_category(STATS_CATEGORY_NAME)
    
    try:
        member_channel_name = MEMBER_COUNT_CHANNEL_NAME.format(count=interaction.guild.member_count)
        bot_channel_name = BOT_COUNT_CHANNEL_NAME.format(count=len([m for m in interaction.guild.members if m.bot]))
        
        member_channel = await interaction.guild.create_voice_channel(member_channel_name, category=category, overwrites={
            interaction.guild.default_role: discord.PermissionOverwrite(connect=False)
        })
        bot_channel = await interaction.guild.create_voice_channel(bot_channel_name, category=category, overwrites={
            interaction.guild.default_role: discord.PermissionOverwrite(connect=False)
        })
        
        bot.stats_channels[interaction.guild.id] = {
            "member_channel_id": member_channel.id,
            "bot_channel_id": bot_channel.id
        }
        
        await interaction.followup.send("✅ Server stats channels have been created!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to create stats channels: {e}", ephemeral=True)

@bot.tree.command(name="backup_data", description="Export bot data to a JSON file")
@app_commands.checks.has_permissions(administrator=True)
async def backup_data(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        data_to_backup = {
            "warnings": warnings_data,
            "auto_roles": auto_roles_data,
            "afk_status": afk_data,
            "giveaways": giveaways_data,
            "templates": templates_data,
            "vouches": vouch_data
        }
        backup_file = discord.File(io.BytesIO(json.dumps(data_to_backup, indent=4).encode()), filename="bot_data_backup.json")
        await interaction.followup.send("✅ Here is the bot's data backup:", file=backup_file, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to create data backup: {e}", ephemeral=True)

# --- DEBUG & SYNC ---
@bot.tree.command(name="sync_commands", description="Admin-only: Manually sync slash commands")
@app_commands.checks.has_permissions(administrator=True)
async def sync_commands(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        synced = await bot.tree.sync()
        await interaction.followup.send(f"✅ Successfully synced {len(synced)} commands!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

@bot.tree.command(name="bot_info", description="Show bot information and status")
async def bot_info(interaction: discord.Interaction):
    embed = create_embed("🤖 Bot Information", None, discord.Color.blue())
    embed.add_field(name="Bot Name", value=bot.user.name, inline=True)
    embed.add_field(name="Bot ID", value=bot.user.id, inline=True)
    embed.add_field(name="Guilds", value=len(bot.guilds), inline=True)
    embed.add_field(name="Commands", value=len(bot.tree.get_commands()), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Sheets Status", value="✅ Connected" if SHEETS_ENABLED else "❌ Disconnected", inline=True)
    commands_list = [f"`/{cmd.name}`" for cmd in bot.tree.get_commands()]
    embed.add_field(name="Available Commands", value=" ".join(commands_list), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- MAIN EXECUTION ---
bot.stats_channels = {}
if __name__ == '__main__':
    # Start the web server in a separate thread
    web_server_thread = threading.Thread(target=web_server, daemon=True)
    web_server_thread.start()
    
    # Run the bot
    token = os.environ.get("Token_Bot")
    if token:
        bot.run(token)
    else:
        print("❌ DISCORD_TOKEN not found in environment variables. Please set it.")


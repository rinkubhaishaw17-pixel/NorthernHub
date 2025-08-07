import discord
from discord.ext import commands, tasks
from discord import app_commands
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, UTC, timezone
import os
import json
import io
import traceback
import asyncio
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

# --- RENDER/REPLIT HEALTH CHECK SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    
    def log_message(self, format, *args):
        # Suppress HTTP server logs
        return

def web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# --- DISCORD BOT SETUP ---
load_dotenv()
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
GIVEAWAY_CHANNEL_NAME = "giveaways"
GIVEAWAY_LOGS_CHANNEL_NAME = "giveaway-logs"
VERIFICATION_CHANNEL_NAME = "verification"
BACKUP_FOLDER = "backups"
WELCOME_CHANNEL_NAME = "welcome"
STATS_CATEGORY_NAME = "📊 Server Stats"
MEMBER_COUNT_CHANNEL_NAME = "Members: {count}"
BOT_COUNT_CHANNEL_NAME = "Bots: {count}"

# --- DATA STORAGE ---
def load_data(filename, default_value=None):
    if default_value is None:
        default_value = {}
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"❌ Error loading {filename}: {e}. Using default value.")
            return default_value
    return default_value

def save_data(filename, data):
    try:
        with open(filename, "w", encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"❌ Error saving {filename}: {e}")

warnings_data = load_data("warnings.json")
auto_roles_data = load_data("auto_roles.json")
afk_data = load_data("afk_status.json")
giveaways_data = load_data("giveaways.json")
templates_data = load_data("templates.json")
vouch_data = load_data("vouch_data.json")
welcome_data = load_data("welcome_config.json")
verification_data = load_data("verification_config.json")
bot.stats_channels = load_data("stats_channels.json")

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
    try:
        sheet = client.open(SPREADSHEET_NAME).sheet1
        SHEETS_ENABLED = True
        print("✅ Google Sheets connection established")
    except gspread.SpreadsheetNotFound:
        print(f"❌ Spreadsheet '{SPREADSHEET_NAME}' not found")
        SHEETS_ENABLED = False
        sheet = None
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
                if product:
                    summary[product] = summary.get(product, 0) + 1
        return summary
    except Exception as e:
        print(f"❌ Error getting stock summary: {e}")
        return {"Error": f"Failed to get stock: {e}"}

# --- HELPER FUNCTIONS ---
async def find_or_create_channel(guild, channel_name, category_name=None):
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if not channel:
        try:
            category = discord.utils.get(guild.categories, name=category_name) if category_name else None
            channel = await guild.create_text_channel(channel_name, category=category)
        except discord.Forbidden:
            print(f"❌ Missing permissions to create channel '{channel_name}' in {guild.name}")
            return None
    return channel

async def log_to_channel(guild, message, channel_name):
    try:
        channel = await find_or_create_channel(guild, channel_name)
        if channel:
            await channel.send(message)
    except Exception as e:
        print(f"❌ Error logging to channel {channel_name}: {e}")

def create_embed(title, description, color, fields=None, thumbnail=None, image=None):
    if isinstance(color, str):
        color = discord.Color.from_str(color)
        
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(UTC))
    
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if image:
        embed.set_image(url=image)

    return embed

def parse_duration(duration_str):
    """Parses a duration string (e.g., '1d', '5h', '30m', '1d 5h 30m') into a timedelta object."""
    duration_regex = re.compile(r'(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?', re.IGNORECASE)
    match = duration_regex.match(duration_str.strip())
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
        giveaway_channel = bot.get_channel(channel_id)
        if not giveaway_channel:
            giveaway_channel = await bot.fetch_channel(channel_id)
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
                    try:
                        end_time_str = ban_reason.split("Tempban until: ")[1].split(" | Reason:")[0].strip()
                        end_time = datetime.fromisoformat(end_time_str)
                        if datetime.now(timezone.utc) >= end_time:
                            await guild.unban(ban_entry.user, reason="Tempban expired")
                            log_channel = await find_or_create_channel(guild, MOD_LOG_CHANNEL_NAME)
                            if log_channel:
                                await log_channel.send(embed=create_embed(
                                    "✅ Tempban Expired",
                                    f"User `{ban_entry.user}`'s tempban has automatically expired.",
                                    discord.Color.green()
                                ))
                    except (ValueError, IndexError) as e:
                        print(f"❌ Error parsing tempban time for {ban_entry.user}: {e}")
        except discord.Forbidden:
            print(f"❌ Missing permissions to manage bans in guild {guild.name}")
        except Exception as e:
            print(f"❌ Error in check_temp_bans for guild {guild.name}: {e}")

@tasks.loop(minutes=1)
async def check_giveaways():
    current_time = datetime.now(timezone.utc)
    for guild in bot.guilds:
        guild_id_str = str(guild.id)
        if guild_id_str not in giveaways_data:
            continue
        
        expired_giveaways = []
        for message_id, giveaway_info in giveaways_data[guild_id_str].items():
            try:
                end_time = datetime.fromisoformat(giveaway_info['end_time'])
                if current_time >= end_time:
                    expired_giveaways.append(giveaway_info)
            except (ValueError, KeyError) as e:
                print(f"❌ Error parsing giveaway end time: {e}")
                expired_giveaways.append(giveaway_info)
        
        for giveaway_info in expired_giveaways:
            await end_giveaway_logic(guild, giveaway_info)

@tasks.loop(hours=6)
async def backup_data_task():
    try:
        if not os.path.exists(BACKUP_FOLDER):
            os.makedirs(BACKUP_FOLDER)
        
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        
        backup_files = []
        for file in os.listdir(BACKUP_FOLDER):
            if file.startswith('complete_backup_') and file.endswith('.json'):
                backup_files.append(os.path.join(BACKUP_FOLDER, file))
        
        backup_files.sort(key=os.path.getctime)
        while len(backup_files) >= 5:
            os.remove(backup_files.pop(0))
        
        backup_data = {
            "warnings": warnings_data,
            "auto_roles": auto_roles_data,
            "giveaways": giveaways_data,
            "vouch_data": vouch_data,
            "welcome_config": welcome_data,
            "verification_config": verification_data,
            "stats_channels": bot.stats_channels,
        }
        
        save_data(os.path.join(BACKUP_FOLDER, f"complete_backup_{timestamp}.json"), backup_data)
        print(f"✅ Data backup complete at {timestamp}")
    except Exception as e:
        print(f"❌ Failed to perform data backup: {e}")

# --- VIEWS & MODALS ---
class WelcomeSetupModal(discord.ui.Modal, title="Setup Welcome Message"):
    title_input = discord.ui.TextInput(
        label="Welcome Title",
        placeholder="Welcome to {server}!",
        required=True,
        default="🎉 Welcome to {server}!"
    )
    message_input = discord.ui.TextInput(
        label="Welcome Message",
        placeholder="Welcome {user} to our amazing server! You are member #{member_count}",
        style=discord.TextStyle.paragraph,
        required=True,
        default="Welcome {user} to our amazing server! You are member #{member_count}"
    )
    image_input = discord.ui.TextInput(
        label="Welcome Image URL (optional)",
        placeholder="https://example.com/image.png",
        required=False
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)
        welcome_data[guild_id] = {
            "title": self.title_input.value,
            "message": self.message_input.value,
            "image_url": self.image_input.value if self.image_input.value else None,
            "enabled": True
        }
        save_data("welcome_config.json", welcome_data)
        await interaction.response.send_message("✅ Welcome message configuration saved!", ephemeral=True)

class VerificationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Verify", style=discord.ButtonStyle.green, emoji="✅", custom_id="verification_button")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = str(interaction.guild.id)
        if guild_id not in verification_data or not verification_data[guild_id].get("enabled", False):
            await interaction.response.send_message("❌ Verification system not configured.", ephemeral=True)
            return
            
        config = verification_data[guild_id]
        verified_role_id = config.get("verified_role_id")
        unverified_role_id = config.get("unverified_role_id")
        
        if not verified_role_id:
            await interaction.response.send_message("❌ Verified role not configured.", ephemeral=True)
            return
            
        verified_role = interaction.guild.get_role(verified_role_id)
        unverified_role = interaction.guild.get_role(unverified_role_id) if unverified_role_id else None
        
        if not verified_role:
            await interaction.response.send_message("❌ Verified role not found.", ephemeral=True)
            return
            
        if verified_role in interaction.user.roles:
            await interaction.response.send_message("✅ You are already verified!", ephemeral=True)
            return
            
        try:
            await interaction.user.add_roles(verified_role, reason="User verification")
            if unverified_role and unverified_role in interaction.user.roles:
                await interaction.user.remove_roles(unverified_role, reason="User verification")
            await interaction.response.send_message("✅ You have been verified! Welcome to the server!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Bot missing permissions to assign roles. Please check role hierarchy.", ephemeral=True)

class VouchModal(discord.ui.Modal, title="Submit a Vouch"):
    product_input = discord.ui.TextInput(
        label="Product Name",
        placeholder="e.g., ProductKey 1",
        required=True
    )
    experience_input = discord.ui.TextInput(
        label="Your Experience",
        placeholder="e.g., The seller was fast and helpful!",
        style=discord.TextStyle.paragraph,
        required=True
    )
    rating_input = discord.ui.TextInput(
        label="Star Rating (1-5)",
        placeholder="e.g., 5",
        required=True
    )
    supporter_input = discord.ui.TextInput(
        label="Staff Member (optional)",
        placeholder="e.g., JohnDoe",
        required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            rating_int = int(self.rating_input.value)
            if not 1 <= rating_int <= 5:
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
                "timestamp": datetime.now(timezone.utc).isoformat()
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
        except Exception as e:
            print(f"❌ Error in vouch submission: {e}")
            await interaction.response.send_message("❌ An error occurred while submitting your vouch.", ephemeral=True)

class VouchPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Submit Vouch", style=discord.ButtonStyle.green, emoji="🏆", custom_id="submit_vouch")
    async def submit_vouch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VouchModal())

class DeliverKeyButtonView(discord.ui.View):
    def __init__(self, user, product):
        super().__init__(timeout=300)
        self.user = user
        self.product = product

    @discord.ui.button(label="Deliver Key", style=discord.ButtonStyle.green, custom_id="deliver_key")
    async def deliver(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You must be an admin to use this button.", ephemeral=True)
            return
        if not SHEETS_ENABLED:
            await interaction.response.send_message("❌ Google Sheets functionality is not available.", ephemeral=True)
            return
        
        button.disabled = True
        await interaction.response.edit_message(view=self)
        
        key = get_key(self.product, f"{self.user.name}#{self.user.discriminator}")
        if key:
            try:
                await self.user.send(
                    f"✅ Thanks for your purchase of **{self.product}**!\nHere is your license key:\n`{key}`"
                )
                
                customer_role = discord.utils.get(interaction.guild.roles, name="Customer")
                buyer_role = discord.utils.get(interaction.guild.roles, name="Buyer")
                member = interaction.guild.get_member(self.user.id)
                
                if member:
                    if customer_role:
                        await member.add_roles(customer_role, reason="Purchase confirmed")
                    if buyer_role and buyer_role in member.roles:
                        await member.remove_roles(buyer_role, reason="Promoted to Customer")
                
                embed = create_embed("Key Delivered", f"Key for **{self.product}** sent to {self.user.mention}", discord.Color.green())
                await interaction.edit_original_response(embed=embed, view=PostPurchaseVouchView(self.user, self.product))
                await log_to_channel(interaction.guild, f"✅ Key manually delivered to `{self.user}` | Product: **{self.product}**", LOG_CHANNEL_NAME)
            except discord.Forbidden:
                await interaction.edit_original_response(content="❌ Failed to DM the user. They may have DMs off.", view=None)
        else:
            await interaction.edit_original_response(content=f"❌ No available keys for **{self.product}**.", view=None)

class PostPurchaseVouchView(discord.ui.View):
    def __init__(self, user, product):
        super().__init__(timeout=None)
        self.user = user
        self.product = product
        
    @discord.ui.button(label="Leave a Vouch", style=discord.ButtonStyle.blurple, emoji="✍️", custom_id="post_purchase_vouch")
    async def leave_vouch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ This button is for the customer who received the key.", ephemeral=True)
            return
        await interaction.response.send_modal(VouchModal(prefilled_product=self.product))

class TicketDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Buy", emoji="💳", description="Purchase products or services"),
            discord.SelectOption(label="Exchange", emoji="🔄", description="Exchange or refund requests"),
            discord.SelectOption(label="Support", emoji="💬", description="General support and help"),
            discord.SelectOption(label="Reseller Apply", emoji="🤝", description="Apply for reseller program"),
            discord.SelectOption(label="Media", emoji="🖼️", description="Media and content related"),
            discord.SelectOption(label="Giveaway", emoji="🎁", description="Giveaway related inquiries"),
        ]
        super().__init__(placeholder="Select ticket reason...", min_values=1, max_values=1, options=options, custom_id="ticket_dropdown")

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        user_name = interaction.user.name.lower().replace(" ", "-")
        user_discriminator = interaction.user.discriminator
        channel_name = f"ticket-{user_name}-{user_discriminator}"
        
        existing = discord.utils.get(guild.text_channels, name=channel_name)
        if existing:
            await interaction.response.send_message("❗ You already have an open ticket.", ephemeral=True)
            return
            
        category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
        if not category:
            try:
                category = await guild.create_category(TICKET_CATEGORY_NAME)
            except discord.Forbidden:
                await interaction.response.send_message("❌ Bot missing permissions to create category.", ephemeral=True)
                return
                
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True)
        }
        
        try:
            channel = await guild.create_text_channel(name=channel_name, category=category, overwrites=overwrites)
            
            ticket_embed = create_embed(
                f"🎫 Ticket: {self.values[0]}",
                f"Ticket created by {interaction.user.mention}\n**Reason:** {self.values[0]}\n\nA staff member will be with you shortly!",
                discord.Color.blue(),
                thumbnail=interaction.user.display_avatar.url
            )
            
            await channel.send(
                f"{interaction.user.mention}",
                embed=ticket_embed,
                view=CloseButtonView()
            )
            await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Failed to create ticket channel: {e}", ephemeral=True)

class CloseButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (interaction.user.guild_permissions.manage_channels or interaction.channel.name.endswith(f"{interaction.user.name}-{interaction.user.discriminator}".lower())):
            await interaction.response.send_message("❌ You can only close your own ticket or need manage channels permission.", ephemeral=True)
            return
            
        await interaction.response.defer()
        
        messages = []
        async for message in interaction.channel.history(limit=100, oldest_first=True):
            timestamp = message.created_at.strftime('%Y-%m-%d %H:%M:%S')
            content = message.content or "[No content]"
            if message.embeds:
                content += f" [Embed: {message.embeds[0].title or 'No title'}]"
            messages.append(f"[{timestamp}] {message.author}: {content}")
        
        transcript_text = "\n".join(messages)
        transcript_file = discord.File(io.BytesIO(transcript_text.encode()), filename=f"transcript-{interaction.channel.name}.txt")
        
        transcript_channel = await find_or_create_channel(interaction.guild, TRANSCRIPT_CHANNEL_NAME)
        if transcript_channel:
            embed = create_embed(
                "📝 Ticket Closed",
                f"**Channel:** {interaction.channel.name}\n**Closed by:** {interaction.user.mention}",
                discord.Color.red()
            )
            await transcript_channel.send(embed=embed, file=transcript_file)
        
        await interaction.followup.send("❌ Ticket will be deleted in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete()

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketDropdown())

class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="Enter Giveaway", style=discord.ButtonStyle.green, custom_id="giveaway_entry")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id_str = str(interaction.guild.id)
        if guild_id_str not in giveaways_data or self.giveaway_id not in giveaways_data[guild_id_str]:
            await interaction.response.send_message("❌ This giveaway is no longer active.", ephemeral=True)
            return
        
        giveaway = giveaways_data[guild_id_str][self.giveaway_id]
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

        try:
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role)
                await interaction.response.send_message(f"✅ Removed the `{role.name}` role.", ephemeral=True)
            else:
                await interaction.user.add_roles(role)
                await interaction.response.send_message(f"✅ You now have the `{role.name}` role!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Bot missing permissions to manage this role.", ephemeral=True)

# --- EVENTS ---
@bot.event
async def on_ready():
    web_server_thread = threading.Thread(target=web_server, daemon=True)
    web_server_thread.start()

    print(f"✅ Logged in as {bot.user}")
    print(f"📊 Bot is in {len(bot.guilds)} guilds")
    
    if SHEETS_ENABLED and not validate_sheet_columns():
        print("❌ Sheet validation failed. Please check column headers.")
        print("⚠️  Google Sheets commands may not work properly.")
    
    bot.add_view(TicketView())
    bot.add_view(VouchPanelView())
    bot.add_view(VerificationView())
    
    for guild in bot.guilds:
        # Check if welcome message exists for this guild
        if str(guild.id) in welcome_data:
            # Recreate welcome message if needed
            welcome_config = welcome_data[str(guild.id)]
            channel = guild.get_channel(welcome_config['channel_id'])
            if channel:
                view = WelcomeView(welcome_config.get('image_url'))
                message = discord.utils.get(await channel.history(limit=50).flatten(), author=bot.user)
                if message and not message.components:
                    await message.edit(view=view)
    
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
    print("🔄 Background tasks started")

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
    guild_id = str(guild.id)
    
    data_files = [
        (warnings_data, "warnings.json"),
        (auto_roles_data, "auto_roles.json"),
        (giveaways_data, "giveaways.json"),
        (welcome_data, "welcome_config.json"),
        (verification_data, "verification_config.json")
    ]
    
    for data_dict, filename in data_files:
        if guild_id in data_dict:
            del data_dict[guild_id]
            save_data(filename, data_dict)

@bot.event
async def on_member_join(member):
    guild = member.guild
    guild_id = str(guild.id)
    
    if guild_id in auto_roles_data:
        for role_id in auto_roles_data[guild_id]:
            role = guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Auto-role on join")
                except discord.Forbidden:
                    print(f"❌ Missing permissions to add role '{role.name}' to {member.name}. Check bot role hierarchy.")
    
    welcome_config = welcome_data.get(guild_id)
    if welcome_config and welcome_config.get("enabled", False):
        welcome_channel = discord.utils.get(guild.text_channels, name=WELCOME_CHANNEL_NAME)
        if welcome_channel:
            # Replace placeholders
            title = welcome_config["title"].replace("{server}", guild.name).replace("{user}", member.display_name)
            message = welcome_config["message"].replace("{server}", guild.name).replace("{user}", member.mention).replace("{member_count}", str(guild.member_count))
            
            embed = create_embed(
                title,
                message,
                discord.Color.from_str(welcome_config['color']),
                thumbnail=member.display_avatar.url,
                image=welcome_config.get('image_url')
            )
            
            try:
                await welcome_channel.send(embed=embed)
            except discord.Forbidden:
                print(f"❌ Missing permissions to send welcome message in {guild.name}")
    
    await update_stats_channels(guild)
    
    if guild_id in verification_data and verification_data[guild_id].get("unverified_role_id"):
        unverified_role = guild.get_role(verification_data[guild_id]["unverified_role_id"])
        if unverified_role:
            try:
                await member.add_roles(unverified_role, reason="Pending verification")
            except discord.Forbidden:
                print(f"❌ Missing permissions to add unverified role to {member.name}. Check bot role hierarchy.")

@bot.event
async def on_member_remove(member):
    guild = member.guild
    await update_stats_channels(guild)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.author.id in afk_data:
        afk_reason = afk_data.pop(message.author.id)
        save_data("afk_status.json", afk_data)
        try:
            await message.channel.send(f"✅ Welcome back, {message.author.mention}! I've removed your AFK status. You were AFK for: **{afk_reason}**", delete_after=10)
        except discord.Forbidden:
            pass

    for mentioned_user in message.mentions:
        if mentioned_user.id in afk_data:
            afk_reason = afk_data[mentioned_user.id]
            embed = create_embed(
                f"💤 {mentioned_user.display_name} is AFK",
                f"Reason: `{afk_reason}`",
                discord.Color.yellow(),
                thumbnail=mentioned_user.display_avatar.url
            )
            try:
                await message.channel.send(embed=embed, delete_after=15)
            except discord.Forbidden:
                pass
            
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
        elif isinstance(error, app_commands.CheckFailure):
            error_msg = "❌ You don't have permission to use this command."

        if not interaction.response.is_done():
            await interaction.response.send_message(error_msg, ephemeral=True)
        else:
            await interaction.followup.send(error_msg, ephemeral=True)
    except Exception as e:
        print(f"❌ Error in error handler: {e}")

# --- WELCOME & VERIFICATION COMMANDS ---
@bot.tree.command(name="setup_welcome", description="Setup welcome message for new members")
@app_commands.describe(
    channel="The channel for welcome messages",
    title="Title of the embed (use {user} and {server})",
    message="Description for the embed (use {user}, {server}, {count})",
    image_url="URL for the image banner (optional)",
    color="Hex color code for the embed (e.g., #00b0f4)"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_welcome(interaction: discord.Interaction, channel: discord.TextChannel, title: str, message: str, color: str = "#00b0f4", image_url: str = None):
    guild_id = str(interaction.guild.id)
    welcome_data[guild_id] = {
        'channel_id': channel.id,
        'title': title,
        'message': message,
        'color': color,
        'image_url': image_url,
        'enabled': True
    }
    save_data("welcome_config.json", welcome_data)

    embed = create_embed(
        "✅ Welcome System Configured",
        f"A welcome message has been set up for {channel.mention}!",
        discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setup_verification", description="Setup verification system")
@app_commands.describe(
    verified_role="Role to give verified members",
    unverified_role="Role to give unverified members (optional)"
)
@app_commands.checks.has_permissions(manage_roles=True)
async def setup_verification(interaction: discord.Interaction, verified_role: discord.Role, unverified_role: discord.Role = None):
    guild_id = str(interaction.guild.id)
    verification_data[guild_id] = {
        "verified_role_id": verified_role.id,
        "unverified_role_id": unverified_role.id if unverified_role else None,
        "enabled": True
    }
    save_data("verification_config.json", verification_data)
    
    embed = create_embed(
        "🔐 Verification Panel",
        "Click the button below to verify yourself and gain access to the server!",
        discord.Color.blue()
    )
    
    await interaction.channel.send(embed=embed, view=VerificationView())
    await interaction.response.send_message("✅ Verification system configured and panel posted!", ephemeral=True)

@bot.tree.command(name="toggle_welcome", description="Enable or disable welcome messages")
@app_commands.describe(enabled="True to enable, False to disable")
@app_commands.checks.has_permissions(manage_guild=True)
async def toggle_welcome(interaction: discord.Interaction, enabled: bool):
    guild_id = str(interaction.guild.id)
    if guild_id not in welcome_data:
        await interaction.response.send_message("❌ Welcome system not configured. Use `/setup_welcome` first.", ephemeral=True)
        return
    
    welcome_data[guild_id]["enabled"] = enabled
    save_data("welcome_config.json", welcome_data)
    
    status = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"✅ Welcome messages have been {status}.", ephemeral=True)

# --- TICKET & PRODUCT KEY COMMANDS ---
@bot.tree.command(name="ticket", description="Open the ticket panel")
@app_commands.checks.has_permissions(manage_channels=True)
async def ticket_panel(interaction: discord.Interaction):
    embed = create_embed(
        "📨 Support Tickets",
        "Need help? Create a support ticket by selecting an option below.\n\n**Available Options:**\n💳 **Buy** - Purchase products or services\n🔄 **Exchange** - Exchange or refund requests\n💬 **Support** - General support and help\n🤝 **Reseller Apply** - Apply for reseller program\n🖼️ **Media** - Media and content related\n🎁 **Giveaway** - Giveaway related inquiries",
        discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, view=TicketView())

@bot.tree.command(name="payment", description="Show payment options")
async def payment_menu(interaction: discord.Interaction):
    embed = create_embed("💳 Payment Methods", "Accepted payment methods:", discord.Color.green())
    embed.add_field(name="💸 PayPal (F&F)", value="paypal@example.com", inline=False)
    embed.add_field(name="🇮🇳 UPI", value="northselling@upi", inline=False)
    embed.add_field(name="💳 Paysafecard", value="DM a staff member for PSC code instructions", inline=False)
    embed.add_field(name="🪙 Cryptocurrency", value="**BTC:** `1ExampleBTC`\n**ETH:** `0xExampleETH`\n**LTC:** `LExampleLTC`", inline=False)
    embed.add_field(name="ℹ️ Important", value="Always use Friends & Family for PayPal to avoid fees!", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="confirm_payment", description="Admin: Confirm payment and deliver key")
@app_commands.describe(user="User to deliver the key to", product="Name of the product")
@app_commands.checks.has_permissions(administrator=True)
async def confirm_payment(interaction: discord.Interaction, user: discord.User, product: str):
    view = DeliverKeyButtonView(user, product)
    embed = create_embed(
        "🔑 Key Delivery",
        f"**User:** {user.mention}\n**Product:** {product}\n\nClick the button below to deliver the key.",
        discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="check_keys", description="Admin: Check product stock")
@app_commands.checks.has_permissions(administrator=True)
async def check_keys(interaction: discord.Interaction):
    if not SHEETS_ENABLED:
        await interaction.response.send_message("❌ Google Sheets functionality is not available.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    summary = get_stock_summary()
    
    embed = create_embed("📦 Product Key Stock", None, discord.Color.orange())
    if "Error" in summary:
        embed.add_field(name="Error", value=summary["Error"], inline=False)
    else:
        if not summary:
            embed.add_field(name="No Products", value="No products found in the spreadsheet.", inline=False)
        else:
            for product, count in summary.items():
                status_emoji = "✅" if count > 10 else "⚠️" if count > 0 else "❌"
                embed.add_field(name=f"{status_emoji} {product}", value=f"{count} key(s) available", inline=True)
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="add_keys", description="Admin: Add new license keys")
@app_commands.describe(product="Product name", keys="Comma-separated list of keys")
@app_commands.checks.has_permissions(administrator=True)
async def add_keys(interaction: discord.Interaction, product: str, keys: str):
    if not SHEETS_ENABLED:
        await interaction.response.send_message("❌ Google Sheets functionality is not available.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    key_list = [key.strip() for key in keys.split(",") if key.strip()]
    
    if not key_list:
        await interaction.followup.send("❌ No valid keys provided.", ephemeral=True)
        return
    
    try:
        headers = sheet.row_values(1)
        next_row = len(sheet.get_all_values()) + 1
        
        for key in key_list:
            sheet.update(f"A{next_row}:D{next_row}", [[product, key, "No", ""]])
            next_row += 1
        
        embed = create_embed(
            "✅ Keys Added Successfully",
            f"Added **{len(key_list)}** key(s) for **{product}**",
            discord.Color.green()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        await log_to_channel(interaction.guild, f"➕ `{interaction.user}` added {len(key_list)} key(s) for **{product}**", LOG_CHANNEL_NAME)
    except Exception as e:
        await interaction.followup.send(f"❌ Error adding keys: {e}", ephemeral=True)

# --- PRODUCT EMBED COMMANDS ---
@bot.tree.command(name="product_embed_template", description="Post a saved template product embed")
@app_commands.describe(template="Name of the saved template", target_channel="Channel to post the embed")
@app_commands.checks.has_permissions(manage_messages=True)
async def product_embed_template(interaction: discord.Interaction, template: str, target_channel: discord.TextChannel):
    if not templates_data or template not in templates_data:
        available = ", ".join(templates_data.keys()) if templates_data else "None"
        await interaction.response.send_message(f"❌ Template not found. Available templates: {available}", ephemeral=True)
        return
    
    data = templates_data[template]

    class ProductEmbedButton(discord.ui.View):
        def __init__(self, ticket_reason):
            super().__init__(timeout=None)
            self.ticket_reason = ticket_reason
            
        @discord.ui.button(label="More Info", style=discord.ButtonStyle.primary, emoji="ℹ️")
        async def info(self, interaction2: discord.Interaction, button: discord.ui.Button):
            guild = interaction2.guild
            user_name = interaction2.user.name.lower().replace(" ", "-")
            channel_name = f"ticket-{user_name}-{interaction2.user.discriminator}"
            
            existing = discord.utils.get(guild.text_channels, name=channel_name)
            if existing:
                await interaction2.response.send_message("❗ You already have an open ticket.", ephemeral=True)
                return
                
            category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
            if not category:
                category = await guild.create_category(TICKET_CATEGORY_NAME)
                
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction2.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True)
            }
            
            channel = await guild.create_text_channel(name=channel_name, category=category, overwrites=overwrites)
            
            ticket_embed = create_embed(
                f"🎫 Ticket: {self.ticket_reason}",
                f"Ticket created by {interaction2.user.mention}\n**Reason:** {self.ticket_reason}\n\nA staff member will be with you shortly!",
                discord.Color.blue(),
                thumbnail=interaction2.user.display_avatar.url
            )
            
            await channel.send(
                f"{interaction2.user.mention}",
                embed=ticket_embed,
                view=CloseButtonView()
            )
            await interaction2.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)

    embed = create_embed(data["title"], data["description"], discord.Color.from_str(data["color"]), image=data["image_url"])
    
    view = ProductEmbedButton(data["ticket_reason"])
    await target_channel.send(embed=embed, view=view)
    await interaction.response.send_message(f"✅ Template embed sent to {target_channel.mention}.", ephemeral=True)

@bot.tree.command(name="save_template", description="Save a new product embed template")
@app_commands.describe(
    name="Template name",
    title="Title",
    description="Description",
    image_url="Image URL",
    ticket_reason="Ticket dropdown label",
    color="Hex color code for the embed (e.g., #00b0f4)"
)
@app_commands.checks.has_permissions(administrator=True)
async def save_template(interaction: discord.Interaction, name: str, title: str, description: str, image_url: str, ticket_reason: str, color: str = "#00b0f4"):
    templates_data[name] = {
        "title": title,
        "description": description,
        "image_url": image_url,
        "ticket_reason": ticket_reason,
        "color": color
    }
    try:
        save_data("templates.json", templates_data)
        await interaction.response.send_message(f"✅ Template `{name}` saved successfully.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error saving template: {e}", ephemeral=True)

@bot.tree.command(name="list_templates", description="List all available product embed templates")
@app_commands.checks.has_permissions(manage_messages=True)
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
@app_commands.checks.has_permissions(manage_messages=True)
async def preview_template(interaction: discord.Interaction, name: str):
    if not templates_data or name not in templates_data:
        await interaction.response.send_message("❌ Template not found.", ephemeral=True)
        return
        
    t = templates_data[name]
    embed = create_embed(t["title"], t["description"], t["color"], image=t["image_url"])
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="product_embed", description="Post a product embed to any channel with a ticket button")
@app_commands.describe(
    title="Product title",
    description="Product description", 
    image_url="URL of the image/banner",
    ticket_reason="Ticket dropdown label to prefill",
    target_channel="Channel to post the embed",
    color="Hex color code for the embed (e.g., #00b0f4)"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def product_embed(interaction: discord.Interaction, title: str, description: str, image_url: str, ticket_reason: str, target_channel: discord.TextChannel, color: str = "#00b0f4"):
    class ProductEmbedButton(discord.ui.View):
        def __init__(self, reason):
            super().__init__(timeout=None)
            self.reason = reason
            
        @discord.ui.button(label="More Info", style=discord.ButtonStyle.primary, emoji="ℹ️")
        async def info(self, interaction2: discord.Interaction, button: discord.ui.Button):
            await interaction2.response.send_message(f"🎫 Creating ticket for **{self.reason}**...", ephemeral=True)

    embed = create_embed(title, description, discord.Color.from_str(color), image=image_url)
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
    
    warnings_data[guild_id][user_id].append({"reason": reason, "moderator": str(interaction.user), "timestamp": datetime.now(timezone.utc).isoformat()})
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
        
    end_time = datetime.now(timezone.utc) + delta
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

# --- DELETION COMMANDS ---
@bot.tree.command(name="clear_messages", description="Delete a specified number of messages from a channel")
@app_commands.describe(amount="The number of messages to delete (1-100)", channel="The channel to clear (defaults to current channel)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear_messages(interaction: discord.Interaction, amount: int, channel: discord.TextChannel = None):
    if amount < 1 or amount > 100:
        await interaction.response.send_message("❌ You can only delete between 1 and 100 messages at a time.", ephemeral=True)
        return

    target_channel = channel or interaction.channel
    await interaction.response.defer(ephemeral=True)

    try:
        deleted_count = await target_channel.purge(limit=amount)
        embed = create_embed(
            "✅ Messages Cleared",
            f"Successfully deleted `{len(deleted_count)}` messages from {target_channel.mention}.",
            discord.Color.green()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ I don't have the permissions to delete messages in that channel.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ An unexpected error occurred: `{e}`", ephemeral=True)


class DeleteConfirmationView(discord.ui.View):
    def __init__(self, target):
        super().__init__(timeout=30)
        self.target = target
        self.confirmed = None

    @discord.ui.button(label="Confirm Deletion", style=discord.ButtonStyle.red)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.send_message("❌ Deletion canceled.", ephemeral=True)
        
    async def on_timeout(self):
        if self.confirmed is None:
            self.confirmed = False
            
@bot.tree.command(name="delete_channel", description="Delete a channel with a confirmation prompt.")
@app_commands.describe(channel="The channel to delete")
@app_commands.checks.has_permissions(manage_channels=True)
async def delete_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.channel.id == channel.id:
        await interaction.response.send_message("❌ You cannot delete the channel you are in.", ephemeral=True)
        return

    embed = create_embed(
        "⚠️ Are you sure?",
        f"This action will permanently delete the channel `{channel.name}`.\nThis cannot be undone.",
        discord.Color.red()
    )
    view = DeleteConfirmationView(channel)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    await view.wait()
    if view.confirmed:
        await interaction.followup.send(f"✅ Deleting channel `{channel.name}`...", ephemeral=True)
        await channel.delete(reason=f"Channel deleted by {interaction.user.name}")
        await log_to_channel(interaction.guild, f"🗑️ Channel **{channel.name}** was deleted by `{interaction.user}`.", MOD_LOG_CHANNEL_NAME)
    else:
        # User cancelled or timed out, but we already sent the cancel message in the view
        pass

@bot.tree.command(name="delete_category", description="Delete a category and all its channels with a confirmation prompt.")
@app_commands.describe(category="The category to delete")
@app_commands.checks.has_permissions(manage_channels=True)
async def delete_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    embed = create_embed(
        "⚠️⚠️ Are you absolutely sure? ⚠️⚠️",
        f"This action will **permanently delete** the category `{category.name}` and all `{len(category.channels)}` channels inside it.\nThis cannot be undone.",
        discord.Color.red()
    )
    view = DeleteConfirmationView(category)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    await view.wait()
    if view.confirmed:
        await interaction.followup.send(f"✅ Deleting category `{category.name}` and all channels...", ephemeral=True)
        for channel in category.channels:
            await channel.delete()
        await category.delete(reason=f"Category deleted by {interaction.user.name}")
        await log_to_channel(interaction.guild, f"🗑️ Category **{category.name}** and all its channels were deleted by `{interaction.user}`.", MOD_LOG_CHANNEL_NAME)
    else:
        pass


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

    end_time = datetime.now(timezone.utc) + delta
    
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
@bot.tree.command(name="add_auto_role", description="Add a role that is automatically assigned to new members")
@app_commands.describe(role="The role to auto-assign")
@app_commands.checks.has_permissions(manage_roles=True)
async def add_auto_role(interaction: discord.Interaction, role: discord.Role):
    # Check if bot can manage this role
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(f"❌ I cannot manage the role `{role.name}` because it's higher than my highest role.", ephemeral=True)
        return
    
    guild_id = str(interaction.guild.id)
    if guild_id not in auto_roles_data:
        auto_roles_data[guild_id] = []
    
    if role.id in auto_roles_data[guild_id]:
        await interaction.response.send_message("❌ This role is already an auto-role.", ephemeral=True)
        return
        
    auto_roles_data[guild_id].append(role.id)
    save_data("auto_roles.json", auto_roles_data)
    
    embed = create_embed(
        "✅ Auto-Role Added",
        f"The role `{role.name}` will now be automatically assigned to new members.",
        discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="remove_auto_role", description="Remove a role from the auto-assign list")
@app_commands.describe(role="The role to remove")
@app_commands.checks.has_permissions(manage_roles=True)
async def remove_auto_role(interaction: discord.Interaction, role: discord.Role):
    guild_id = str(interaction.guild.id)
    if guild_id in auto_roles_data and role.id in auto_roles_data[guild_id]:
        auto_roles_data[guild_id].remove(role.id)
        save_data("auto_roles.json", auto_roles_data)
        
        embed = create_embed(
            "✅ Auto-Role Removed",
            f"The role `{role.name}` has been removed from the auto-role list.",
            discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message("❌ That role is not currently an auto-role.", ephemeral=True)

@bot.tree.command(name="list_auto_roles", description="List all roles that are automatically assigned")
@app_commands.checks.has_permissions(manage_roles=True)
async def list_auto_roles(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    if guild_id not in auto_roles_data or not auto_roles_data[guild_id]:
        await interaction.response.send_message("❌ No auto-roles are set up for this server.", ephemeral=True)
        return
        
    roles = []
    # Create a temporary list to hold valid roles, to avoid modifying the list while iterating
    valid_role_ids = []
    for role_id in auto_roles_data[guild_id]:
        role = interaction.guild.get_role(role_id)
        if role:
            roles.append(f"• {role.name} ({role.mention})")
            valid_role_ids.append(role_id)
    
    if len(valid_role_ids) != len(auto_roles_data[guild_id]):
        auto_roles_data[guild_id] = valid_role_ids
        save_data("auto_roles.json", auto_roles_data)
        
    if roles:
        embed = create_embed(
            "🤖 Auto-Roles",
            "\n".join(roles),
            discord.Color.blurple()
        )
        embed.set_footer(text=f"Total: {len(roles)} role(s)")
    else:
        embed = create_embed(
            "🤖 Auto-Roles",
            "No valid auto-roles configured.",
            discord.Color.blurple()
        )
        
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="afk", description="Set your AFK status")
@app_commands.describe(reason="The reason you are AFK")
async def afk(interaction: discord.Interaction, reason: str = "I am AFK."):
    user_id = interaction.user.id
    if user_id in afk_data:
        await interaction.response.send_message("❗ You are already AFK. Your status was updated.", ephemeral=True)
    else:
        await interaction.response.send_message(f"✅ You are now AFK. Reason: `{reason}`", ephemeral=True)
    
    afk_data[user_id] = reason
    save_data("afk_status.json", afk_data)

@bot.tree.command(name="userinfo", description="Get information about a user")
@app_commands.describe(user="The user to get information about (optional)")
async def userinfo(interaction: discord.Interaction, user: discord.Member = None):
    member = user or interaction.user
    
    account_age = datetime.now(timezone.utc) - member.created_at
    server_age = datetime.now(timezone.utc) - member.joined_at if member.joined_at else None
    
    embed = create_embed(
        f"👤 User Info: {member.display_name}",
        f"**Username:** {member.name}#{member.discriminator}\n**Display Name:** {member.display_name}",
        discord.Color.blue(),
        thumbnail=member.display_avatar.url
    )
    
    embed.add_field(name="🆔 User ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="📅 Account Created", value=f"<t:{int(member.created_at.timestamp())}:F>\n({account_age.days} days ago)", inline=False)
    
    if member.joined_at:
        embed.add_field(name="📥 Joined Server", value=f"<t:{int(member.joined_at.timestamp())}:F>\n({server_age.days} days ago)", inline=False)
    
    status_emoji = {
        discord.Status.online: "🟢",
        discord.Status.idle: "🟡",
        discord.Status.dnd: "🔴",
        discord.Status.offline: "⚫"
    }
    embed.add_field(name="📡 Status", value=f"{status_emoji.get(member.status, '❓')} {member.status.name.title()}", inline=True)
    
    roles = [role.mention for role in member.roles if role.name != "@everyone"]
    if roles:
        roles_text = ", ".join(roles) if len(", ".join(roles)) <= 1024 else f"{len(roles)} roles"
        embed.add_field(name=f"🏷️ Roles ({len(roles)})", value=roles_text, inline=False)
    
    key_perms = []
    if member.guild_permissions.administrator:
        key_perms.append("Administrator")
    elif member.guild_permissions.manage_guild:
        key_perms.append("Manage Server")
    elif member.guild_permissions.manage_channels:
        key_perms.append("Manage Channels")
    elif member.guild_permissions.manage_messages:
        key_perms.append("Manage Messages")
    
    if key_perms:
        embed.add_field(name="🔑 Key Permissions", value=", ".join(key_perms), inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="serverinfo", description="Get information about the server")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    
    online = len([m for m in guild.members if m.status == discord.Status.online])
    idle = len([m for m in guild.members if m.status == discord.Status.idle])
    dnd = len([m for m in guild.members if m.status == discord.Status.dnd])
    offline = len([m for m in guild.members if m.status == discord.Status.offline])
    
    text_channels = len(guild.text_channels)
    voice_channels = len(guild.voice_channels)
    categories = len(guild.categories)
    
    embed = create_embed(
        f"🏠 Server Info: {guild.name}",
        guild.description or "No server description set.",
        discord.Color.blurple(),
        thumbnail=guild.icon.url if guild.icon else None
    )
    
    embed.add_field(name="👑 Owner", value=guild.owner.mention if guild.owner else "Unknown", inline=True)
    embed.add_field(name="🆔 Server ID", value=f"`{guild.id}`", inline=True)
    embed.add_field(name="📅 Created", value=f"<t:{int(guild.created_at.timestamp())}:F>", inline=False)
    
    embed.add_field(
        name=f"👥 Members ({guild.member_count})",
        value=f"🟢 {online} | 🟡 {idle} | 🔴 {dnd} | ⚫ {offline}\n🤖 Bots: {len([m for m in guild.members if m.bot])}",
        inline=True
    )
    
    embed.add_field(
        name=f"📁 Channels ({len(guild.channels)})",
        value=f"💬 Text: {text_channels}\n🔊 Voice: {voice_channels}\n📋 Categories: {categories}",
        inline=True
    )
    
    embed.add_field(
        name="📊 Other",
        value=f"🏷️ Roles: {len(guild.roles)}\n😀 Emojis: {len(guild.emojis)}\n⚡ Boosts: {guild.premium_subscription_count}",
        inline=True
    )
    
    verification_level = {
        discord.VerificationLevel.none: "None",
        discord.VerificationLevel.low: "Low",
        discord.VerificationLevel.medium: "Medium",
        discord.VerificationLevel.high: "High",
        discord.VerificationLevel.highest: "Highest"
    }
    
    embed.add_field(
        name="🛡️ Security",
        value=f"Verification: {verification_level.get(guild.verification_level, 'Unknown')}",
        inline=True
    )
    
    if guild.banner:
        embed.set_image(url=guild.banner.url)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="poll", description="Create a poll with up to 10 options")
@app_commands.describe(
    question="The poll question",
    option1="Option 1", option2="Option 2", option3="Option 3 (optional)",
    option4="Option 4 (optional)", option5="Option 5 (optional)"
)
async def poll(interaction: discord.Interaction, question: str, option1: str, option2: str, 
               option3: str = None, option4: str = None, option5: str = None):
    
    options = [opt for opt in [option1, option2, option3, option4, option5] if opt is not None]
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    
    if len(options) < 2:
        await interaction.response.send_message("❌ You need at least 2 options for a poll.", ephemeral=True)
        return
    
    poll_description = "\n".join([f"{emojis[i]} {opt}" for i, opt in enumerate(options)])
    
    embed = create_embed(
        f"📊 Poll: {question}",
        poll_description,
        discord.Color.purple()
    )
    embed.add_field(name="How to vote:", value="React with the corresponding emoji below!", inline=False)
    embed.set_footer(text=f"Poll created by {interaction.user.display_name}")
    
    await interaction.response.send_message(embed=embed)
    message = await interaction.original_response()
    
    for i in range(len(options)):
        try:
            await message.add_reaction(emojis[i])
        except discord.HTTPException:
            pass

@bot.tree.command(name="announce", description="Make a professional announcement")
@app_commands.describe(
    channel="Channel to post in",
    title="Announcement title",
    message="The announcement message",
    ping_everyone="Whether to ping @everyone (optional)"
)
@app_commands.checks.has_permissions(mention_everyone=True)
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, title: str, message: str, ping_everyone: bool = False):
    embed = create_embed(
        f"📢 {title}",
        message,
        discord.Color.gold()
    )
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text=f"Announced in #{channel.name}")
    
    content = "@everyone" if ping_everyone else None
    
    try:
        await channel.send(content=content, embed=embed)
        
        success_embed = create_embed(
            "✅ Announcement Sent",
            f"Your announcement has been posted in {channel.mention}",
            discord.Color.green()
        )
        await interaction.response.send_message(embed=success_embed, ephemeral=True)
        
    except discord.Forbidden:
        await interaction.response.send_message(f"❌ I don't have permission to send messages in {channel.mention}", ephemeral=True)

# --- NEW FEATURES ---
@bot.tree.command(name="create_tos", description="Post the server's Terms of Service")
@app_commands.describe(channel="The channel to post the ToS in (defaults to current channel)")
@app_commands.checks.has_permissions(administrator=True)
async def create_tos(interaction: discord.Interaction, channel: discord.TextChannel = None):
    target_channel = channel or interaction.channel
    
    # Using more visually appealing emojis and formatting
    tos_text = (
        "--- **NorthernHub Terms of Service** ---\n\n"
        "By purchasing, interacting, or using our services, you agree to the following terms and conditions.\n\n"
        "**📜 1. No Refunds**\n"
        "All sales are final. We do not offer refunds under any circumstances.\n\n"
        "**💳 2. Payment Policy**\n"
        "• All payments must be sent via PayPal using **Friends and Family**.\n"
        "• Do not include any messages or notes with your payment.\n"
        "• Failure to follow this policy will result in no product delivery.\n\n"
        "**⚖️ 3. Vouch & Review Policy**\n"
        "• We reserve the right to refuse product delivery if the wrong product name is mentioned in a vouch.\n"
        "• Any vouch found to be misleading may result in a permanent ban.\n"
        "• Users who spam words in a vouch will not receive a product.\n\n"
        "**🛡️ 4. Product & Warranty**\n"
        "• We are not responsible for products being revoked unless they are explicitly sold with a 'warranty'.\n"
        "• Any codes provided through tickets are not accepted for refunds.\n"
        "• You are responsible for securing purchased accounts/products immediately after the transaction.\n\n"
        "**🚫 5. Server Conduct & Disclaimers**\n"
        "• **Accusations of Scamming:** Accusing us of scamming will result in an immediate and permanent ban.\n"
        "• **Anti-Spam Policy:** Any type of spam will result in a ban and loss of product access.\n"
        "• **Direct Messaging:** Directly messaging staff regarding support or orders will result in a permanent ban.\n"
        "• **Server Departure:** If you leave our server, your purchased product will be revoked.\n"
        "• **Delivery Time:** We do not provide an exact delivery time for products; times may vary.\n\n"
        "***Note:*** Violation of these terms will result in appropriate action."
    )
    
    embed = create_embed(
        "📝 NorthernHub Terms of Service",
        tos_text,
        discord.Color.from_str("#5865F2") # A vibrant, custom color
    )
    
    try:
        await target_channel.send(embed=embed)
        await interaction.response.send_message(f"✅ Terms of Service embed has been posted in {target_channel.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to send messages in that channel.", ephemeral=True)


# --- MAIN EXECUTION ---
if __name__ == '__main__':
    web_server_thread = threading.Thread(target=web_server, daemon=True)
    web_server_thread.start()
    
    try:
        token = os.environ.get("DISCORD_TOKEN")
        if token:
            bot.run(token)
        else:
            print("❌ DISCORD_TOKEN environment variable not set. Please add it to your Replit secrets or .env file.")
    except Exception as e:
        print(f"❌ An error occurred while running the bot: {e}")


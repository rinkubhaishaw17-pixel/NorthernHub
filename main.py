import discord
from discord.ext import commands
from discord import app_commands
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import os
import json
import io
import traceback

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

LOG_CHANNEL_NAME = "ticket-logs"
TRANSCRIPT_CHANNEL_NAME = "transcripts"
VOUCH_CHANNEL_NAME = "vouches"
TICKET_CATEGORY_NAME = "📂 Tickets"
SPREADSHEET_NAME = "ProductKeys"
CREDENTIALS_FILE = "google-credentials.json"

# Initialize Google Sheets connection with error handling
try:
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

async def log_to_channel(guild, message, channel_name):
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if not channel:
        category = discord.utils.get(guild.categories, name="📁 Logs")
        channel = await guild.create_text_channel(channel_name, category=category if category else None)
    await channel.send(message)

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
                
                embed = discord.Embed(title="Key Delivered", description=f"Key for **{self.product}** sent to {self.user.mention}", color=discord.Color.green())
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    
                await log_to_channel(interaction.guild, f"✅ Key manually delivered to `{self.user}` | Product: **{self.product}**", LOG_CHANNEL_NAME)
                
            except discord.Forbidden:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Failed to DM the user. They may have DMs off.", ephemeral=True)
                else:
                    await interaction.followup.send("❌ Failed to DM the user. They may have DMs off.", ephemeral=True)
        else:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ No available keys for **{self.product}**.", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ No available keys for **{self.product}**.", ephemeral=True)

class TicketDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Buy", emoji="💳"),
            discord.SelectOption(label="Exchange", emoji="💠"),
            discord.SelectOption(label="Support", emoji="🚠"),
            discord.SelectOption(label="Reseller Apply", emoji="📩"),
            discord.SelectOption(label="Media", emoji="📸"),
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
        await log_to_channel(guild, f"🔞 Ticket opened by `{interaction.user}` for **{self.values[0]}** in {channel.mention}", LOG_CHANNEL_NAME)

class CloseButtonView(discord.ui.View):
    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        messages = []
        async for message in interaction.channel.history(limit=100, oldest_first=True):
            messages.append(f"[{message.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {message.author}: {message.content}")

        transcript_text = "\n".join(messages)
        transcript_file = discord.File(io.BytesIO(transcript_text.encode()), filename=f"transcript-{interaction.channel.name}.txt")

        await log_to_channel(interaction.guild, f"📝 Transcript for `{interaction.channel.name}` (closed by {interaction.user}):", LOG_CHANNEL_NAME)
        log_channel = discord.utils.get(interaction.guild.text_channels, name=TRANSCRIPT_CHANNEL_NAME)
        if not log_channel:
            log_channel = await interaction.guild.create_text_channel(TRANSCRIPT_CHANNEL_NAME)
        if log_channel:
            await log_channel.send(file=transcript_file)

        await interaction.response.send_message("❌ Ticket closed and transcript saved.", ephemeral=True)
        await interaction.channel.delete()

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(TicketDropdown())

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    print(f"📊 Bot is in {len(bot.guilds)} guilds")
    
    if SHEETS_ENABLED and not validate_sheet_columns():
        print("❌ Sheet validation failed. Please check column headers.")
        print("⚠️  Google Sheets commands may not work properly.")
    
    try:
        # Sync commands globally (works across all servers)
        synced = await bot.tree.sync()
        print(f"🔁 Synced {len(synced)} commands globally.")
        
        # List all commands that were synced
        for command in synced:
            print(f"   - /{command.name}: {command.description}")
            
    except Exception as e:
        print(f"❌ Global sync error: {e}")
        print(f"❌ Full error: {traceback.format_exc()}")
        
    # Also show which guilds the bot is in
    print("🏠 Bot is active in:")
    for guild in bot.guilds:
        print(f"   - {guild.name} (ID: {guild.id})")

@bot.event
async def on_guild_join(guild):
    print(f"🎉 Bot joined new guild: {guild.name} (ID: {guild.id})")
    try:
        # Optionally sync commands when joining a new guild
        synced = await bot.tree.sync()
        print(f"🔁 Re-synced {len(synced)} commands after joining {guild.name}")
    except Exception as e:
        print(f"❌ Sync error after joining {guild.name}: {e}")

@bot.event
async def on_guild_remove(guild):
    print(f"👋 Bot left guild: {guild.name} (ID: {guild.id})")

@bot.event
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Global error handler for slash commands"""
    error_msg = f"❌ An error occurred: {str(error)}"
    print(f"Command error in {interaction.guild.name if interaction.guild else 'DM'}: {error}")
    print(f"Full traceback: {traceback.format_exc()}")
    
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(error_msg, ephemeral=True)
        else:
            await interaction.followup.send(error_msg, ephemeral=True)
    except:
        pass  # If we can't send the error message, just log it

@bot.tree.command(name="ticket", description="Open the ticket panel")
async def ticket_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📨 Tickets",
        description="Create a support ticket by selecting an option below.",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, view=TicketView())

@bot.tree.command(name="payment", description="Show payment options")
async def payment_menu(interaction: discord.Interaction):
    embed = discord.Embed(title="💳 Payment Methods", color=discord.Color.green())
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
    try:
        await interaction.response.send_message(f"✅ Use the button below to deliver a key to {user.mention} for **{product}**.", view=view, ephemeral=True)
    except discord.errors.NotFound:
        await interaction.followup.send(f"✅ Use the button below to deliver a key to {user.mention} for **{product}**.", view=view, ephemeral=True)

@bot.tree.command(name="check_keys", description="Admin-only: Check product stock")
async def check_keys(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You must be an admin to use this command.", ephemeral=True)
        return

    if not SHEETS_ENABLED:
        await interaction.response.send_message("❌ Google Sheets functionality is not available.", ephemeral=True)
        return

    summary = get_stock_summary()
    embed = discord.Embed(title="📦 Product Key Stock", color=discord.Color.orange())
    
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

def load_templates():
    try:
        with open("templates.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}

@bot.tree.command(name="product_embed_template", description="Post a saved template product embed")
@app_commands.describe(template="Name of the saved template", target_channel="Channel to post the embed")
async def product_embed_template(interaction: discord.Interaction, template: str, target_channel: discord.TextChannel):
    templates = load_templates()

    if template not in templates:
        await interaction.response.send_message("❌ Template not found.", ephemeral=True)
        return

    data = templates[template]

    class ProductEmbedButton(discord.ui.View):
        @discord.ui.button(label="More Info", style=discord.ButtonStyle.primary)
        async def info(self, interaction2: discord.Interaction, button: discord.ui.Button):
            # Create a ticket based on the template's ticket reason
            await interaction2.response.send_message(f"🎫 Creating ticket for **{data['ticket_reason']}**...", ephemeral=True)

    embed = discord.Embed(title=data["title"], description=data["description"], color=discord.Color.blurple())
    embed.set_image(url=data["image_url"])
    await target_channel.send(embed=embed, view=ProductEmbedButton())
    await interaction.response.send_message(f"✅ Template embed sent to {target_channel.mention}.", ephemeral=True)

@bot.tree.command(name="save_template", description="Save a new product embed template")
@app_commands.describe(name="Template name", title="Title", description="Description", image_url="Image URL", ticket_reason="Ticket dropdown label")
async def save_template(interaction: discord.Interaction, name: str, title: str, description: str, image_url: str, ticket_reason: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    templates = load_templates()
    templates[name] = {
        "title": title,
        "description": description,
        "image_url": image_url,
        "ticket_reason": ticket_reason
    }

    try:
        with open("templates.json", "w") as f:
            json.dump(templates, f, indent=4)
        await interaction.response.send_message(f"✅ Template `{name}` saved.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error saving template: {e}", ephemeral=True)

@bot.tree.command(name="edit_template", description="Edit an existing product embed template")
@app_commands.describe(name="Template name to edit", field="Field to update", value="New value")
async def edit_template(interaction: discord.Interaction, name: str, field: str, value: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    templates = load_templates()
    if not templates:
        await interaction.response.send_message("❌ Failed to load templates.", ephemeral=True)
        return

    if name not in templates or field not in templates[name]:
        await interaction.response.send_message("❌ Template or field not found.", ephemeral=True)
        return

    templates[name][field] = value

    try:
        with open("templates.json", "w") as f:
            json.dump(templates, f, indent=4)
        await interaction.response.send_message(f"✏️ Updated `{field}` for template `{name}`.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error updating template: {e}", ephemeral=True)

@bot.tree.command(name="list_templates", description="List all available product embed templates")
async def list_templates(interaction: discord.Interaction):
    templates = load_templates()
    if not templates:
        await interaction.response.send_message("❌ No templates found.", ephemeral=True)
        return

    embed = discord.Embed(title="🧩 Available Embed Templates", description="Use `/product_embed_template` with one of these:", color=discord.Color.teal())
    for name, data in templates.items():
        embed.add_field(name=name, value=data.get("title", "No title"), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="preview_template", description="Preview a saved product template without posting")
@app_commands.describe(name="Template name to preview")
async def preview_template(interaction: discord.Interaction, name: str):
    templates = load_templates()
    if not templates:
        await interaction.response.send_message("❌ Failed to load templates.", ephemeral=True)
        return

    if name not in templates:
        await interaction.response.send_message("❌ Template not found.", ephemeral=True)
        return

    t = templates[name]
    embed = discord.Embed(title=t["title"], description=t["description"], color=discord.Color.blurple())
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
        @discord.ui.button(label="More Info", style=discord.ButtonStyle.primary)
        async def info(self, interaction2: discord.Interaction, button: discord.ui.Button):
            await interaction2.response.send_message(f"🎫 Creating ticket for **{ticket_reason}**...", ephemeral=True)

    embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    embed.set_image(url=image_url)
    await target_channel.send(embed=embed, view=ProductEmbedButton())
    await interaction.response.send_message(f"✅ Product embed sent to {target_channel.mention}.", ephemeral=True)

@bot.tree.command(name="vouch", description="Submit a vouch for a product or service")
@app_commands.describe(product="Product name", experience="Your experience", rating="Star rating (1-5)", supporter="Staff member who helped (optional)")
async def vouch(interaction: discord.Interaction, product: str, experience: str, rating: int, supporter: str = None):
    if rating < 1 or rating > 5:
        await interaction.response.send_message("⭐ Rating must be between 1 and 5.", ephemeral=True)
        return

    stars = "⭐" * rating
    embed = discord.Embed(title=f"🏆 Vouch from {interaction.user}", color=discord.Color.purple())
    embed.add_field(name="Product", value=f"`{product}`", inline=True)
    embed.add_field(name="Star Rating", value=stars, inline=True)
    if supporter:
        embed.add_field(name="Supporter", value=supporter, inline=False)
    embed.add_field(name="Experience", value=f"```{experience}```", inline=False)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    vouch_channel = discord.utils.get(interaction.guild.text_channels, name=VOUCH_CHANNEL_NAME)
    if vouch_channel:
        await vouch_channel.send(embed=embed)
        await interaction.response.send_message("✅ Your vouch has been submitted!", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Vouch channel not found.", ephemeral=True)

# Add a manual sync command for testing/debugging
@bot.tree.command(name="sync_commands", description="Admin-only: Manually sync slash commands")
async def sync_commands(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You must be an admin to use this command.", ephemeral=True)
        return
    
    try:
        await interaction.response.defer(ephemeral=True)
        synced = await bot.tree.sync()
        await interaction.followup.send(f"✅ Successfully synced {len(synced)} commands!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

# Add a debug command to check bot status
@bot.tree.command(name="bot_info", description="Show bot information and status")
async def bot_info(interaction: discord.Interaction):
    embed = discord.Embed(title="🤖 Bot Information", color=discord.Color.blue())
    embed.add_field(name="Bot Name", value=bot.user.name, inline=True)
    embed.add_field(name="Bot ID", value=bot.user.id, inline=True)
    embed.add_field(name="Guilds", value=len(bot.guilds), inline=True)
    embed.add_field(name="Commands", value=len(bot.tree.get_commands()), inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Sheets Status", value="✅ Connected" if SHEETS_ENABLED else "❌ Disconnected", inline=True)
    
    # List all available commands
    commands_list = [f"/{cmd.name}" for cmd in bot.tree.get_commands()]
    embed.add_field(name="Available Commands", value="\n".join(commands_list), inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Replace this with your actual bot token
bot.run("MTQwMTU0NTA1MDgzOTg0MjkxMg.GBfHJa.5cCWobGio9U-43vxRrfBS_RCYC_naAvO28CxYU")
# A complete Discord security bot replica with automatic verification features.
# This bot uses nextcord, a modern fork of discord.py.
# You will need to install the following libraries:
# pip install nextcord Pillow cryptography

import nextcord
from nextcord.ext import commands
import asyncio
from typing import Dict, Any, Optional
from PIL import Image, ImageDraw, ImageFont
import random
import io
import datetime
import time
import json
import os
from cryptography.fernet import Fernet
from nextcord import ButtonStyle, Interaction
from nextcord.ui import View, Button

# --- Bot Configuration ---
# Create a config.py file in the same directory and add the following:
# DISCORD_TOKEN = "YOUR_BOT_TOKEN_HERE"
# GUILD_ID = 123456789012345678 # Your server's guild ID

try:
    from config import DISCORD_TOKEN, GUILD_ID
except ImportError:
    print("Please create a config.py file with your DISCORD_TOKEN and GUILD_ID.")
    exit()

# --- Global State Management ---
# Using a local JSON file for persistence. For a production bot, a database is highly recommended.
guild_settings: Dict[int, Any] = {}
captcha_codes: Dict[int, str] = {}
member_join_times: Dict[int, datetime.datetime] = {}
recent_joins: list[datetime.datetime] = []
captcha_cooldowns: Dict[int, float] = {}

# --- Persistence Functions ---
# These functions handle loading and saving the settings to a local JSON file.
SETTINGS_FILE = "data.json"

def load_settings():
    """Loads settings from a local JSON file."""
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            try:
                data = json.load(f)
                # Convert guild ID string keys back to integers
                return {int(k): v for k, v in data.items()}
            except json.JSONDecodeError:
                print("Error loading settings from JSON file. Starting with empty settings.")
                return {}
    return {}

def save_settings():
    """Saves settings to a local JSON file."""
    with open(SETTINGS_FILE, "w") as f:
        json.dump(guild_settings, f, indent=4)
        print("Settings saved to data.json.")

# --- Bot Intents ---
# These are the permissions the bot needs to function.
intents = nextcord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# --- Captcha Generation ---
def generate_captcha():
    code = ''.join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", k=6))
    image = Image.new('RGB', (200, 100), color='white')
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except IOError:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(image)
    draw.text((20, 20), code, font=font, fill='black')
    
    for _ in range(500):
        x = random.randint(0, 199)
        y = random.randint(0, 99)
        draw.point((x, y), fill=(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))
    
    byte_io = io.BytesIO()
    image.save(byte_io, 'PNG')
    byte_io.seek(0)
    return code, byte_io

# --- Helper Functions for Logging ---
async def log_event(guild: nextcord.Guild, message: str):
    settings = guild_settings.get(guild.id)
    if settings and settings.get("log_channel"):
        log_channel = guild.get_channel(settings.get("log_channel"))
        if log_channel:
            embed = nextcord.Embed(description=message, color=nextcord.Color.blue())
            await log_channel.send(embed=embed)
    print(f"[LOG] {guild.name}: {message}")

# --- Verification Button Views ---
class VerifyView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @nextcord.ui.button(label="Verify", style=ButtonStyle.green, custom_id="persistent_verify_button")
    async def verify_button(self, button: Button, interaction: Interaction):
        guild = interaction.guild
        member = interaction.user

        settings = guild_settings.get(guild.id)
        if not settings:
            await interaction.response.send_message("The verification system is not set up.", ephemeral=True)
            return

        verified_role_id = settings.get("verified_role")
        unverified_role_id = settings.get("unverified_role")

        if not verified_role_id or not unverified_role_id:
            await interaction.response.send_message("Verification roles are not configured properly.", ephemeral=True)
            return

        verified_role = guild.get_role(verified_role_id)
        unverified_role = guild.get_role(unverified_role_id)

        if verified_role in member.roles:
            await interaction.response.send_message("You are already verified! Welcome back.", ephemeral=True)
            return

        if unverified_role not in member.roles:
            await interaction.response.send_message("You are not in the unverified role. If you believe this is an error, please contact an admin.", ephemeral=True)
            return

        try:
            await member.remove_roles(unverified_role)
            await member.add_roles(verified_role)
            await interaction.response.send_message("Verification successful! You now have access to the server.", ephemeral=True)
            await log_event(guild, f"✅ `{member.name}` has been verified via one-click button.")
        except nextcord.Forbidden:
            await interaction.response.send_message("I don't have the permissions to manage your roles. Please contact a server admin.", ephemeral=True)

class CaptchaView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @nextcord.ui.button(label="Start Verification", style=ButtonStyle.blurple, custom_id="persistent_captcha_button")
    async def start_captcha_button(self, button: Button, interaction: Interaction):
        user = interaction.user
        guild = interaction.guild

        cooldown_duration = 30
        if user.id in captcha_cooldowns and (time.time() - captcha_cooldowns[user.id]) < cooldown_duration:
            remaining_time = int(cooldown_duration - (time.time() - captcha_cooldowns[user.id]))
            await interaction.response.send_message(f"Please wait {remaining_time} seconds before trying again.", ephemeral=True)
            return
        
        if user.id in captcha_codes:
            await interaction.response.send_message("You are already in the process of a captcha verification. Please check your DMs.", ephemeral=True)
            return

        settings = guild_settings.get(guild.id)
        if not settings:
            await interaction.response.send_message("The verification system is not set up.", ephemeral=True)
            return

        unverified_role_id = settings.get("unverified_role")
        unverified_role = guild.get_role(unverified_role_id)

        if unverified_role not in user.roles:
            await interaction.response.send_message("You are already verified!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            code, image_bytes = generate_captcha()
            captcha_codes[user.id] = code
            captcha_cooldowns[user.id] = time.time()

            captcha_file = nextcord.File(image_bytes, filename="captcha.png")
            embed = nextcord.Embed(
                title="Captcha Verification",
                description="Please type the code you see in the image below into this chat. You have 60 seconds."
            )
            embed.set_image(url="attachment://captcha.png")

            await user.send(file=captcha_file, embed=embed)
            await interaction.followup.send("A captcha has been sent to your DMs. Please check your DMs to complete verification.", ephemeral=True)
            await log_event(guild, f"🪪 Sent captcha to `{user.name}`.")

            await asyncio.sleep(60)

            if user.id in captcha_codes:
                del captcha_codes[user.id]
                try:
                    await user.send("You took too long to respond. Please start the captcha process again.")
                    await log_event(guild, f"⏰ Captcha for `{user.name}` timed out.")
                except nextcord.Forbidden:
                    pass

        except nextcord.Forbidden:
            await interaction.followup.send("I couldn't send you a DM. Please enable DMs for this server in your privacy settings and try again.", ephemeral=True)
            if user.id in captcha_codes:
                del captcha_codes[user.id]

# --- Bot Commands ---
@bot.slash_command(
    name="setup_verification",
    description="Sets up the verification system for the server.",
    guild_ids=[GUILD_ID]
)
@commands.has_permissions(manage_guild=True)
async def setup_verification(
    interaction: Interaction,
    verification_channel: nextcord.TextChannel,
    unverified_role: nextcord.Role,
    verified_role: nextcord.Role,
    log_channel: nextcord.TextChannel,
    verification_type: str = nextcord.SlashOption(
        name="type",
        description="Choose the verification type.",
        choices={"Button Click": "button", "Captcha": "captcha"}
    ),
    kick_timer: Optional[int] = nextcord.SlashOption(
        name="kick_timer",
        description="Time in minutes before an unverified user is kicked (0 to disable).",
        required=False,
        default=2
    )
):
    settings = {
        "verification_channel": verification_channel.id,
        "unverified_role": unverified_role.id,
        "verified_role": verified_role.id,
        "log_channel": log_channel.id,
        "verification_type": verification_type,
        "kick_timer": kick_timer
    }
    guild_settings[interaction.guild.id] = settings
    save_settings() # Save settings after setup

    if verification_type == "button":
        view = VerifyView()
        embed = nextcord.Embed(
            title="Verification Required",
            description="Welcome! To gain full access, please click the button below to verify yourself.",
            color=nextcord.Color.green()
        )
        await verification_channel.send(embed=embed, view=view)
        await interaction.response.send_message("Verification system with a one-click button has been set up!", ephemeral=True)
    elif verification_type == "captcha":
        view = CaptchaView()
        embed = nextcord.Embed(
            title="Verification Required",
            description="Welcome! To gain full access, please click the button below to start the captcha verification.",
            color=nextcord.Color.blurple()
        )
        await verification_channel.send(embed=embed, view=view)
        await interaction.response.send_message("Verification system with captcha has been set up!", ephemeral=True)

    await log_event(interaction.guild, f"⚙️ Verification system set up by `{interaction.user.name}`.")

@bot.slash_command(
    name="force_verify",
    description="Manually verifies a member.",
    guild_ids=[GUILD_ID]
)
@commands.has_permissions(manage_roles=True)
async def force_verify(
    interaction: Interaction,
    member: nextcord.Member
):
    settings = guild_settings.get(interaction.guild.id)
    if not settings:
        await interaction.response.send_message("The verification system is not set up.", ephemeral=True)
        return

    verified_role = interaction.guild.get_role(settings.get("verified_role"))
    unverified_role = interaction.guild.get_role(settings.get("unverified_role"))

    if not verified_role or not unverified_role:
        await interaction.response.send_message("Verification roles are not configured properly.", ephemeral=True)
        return

    if verified_role in member.roles:
        await interaction.response.send_message(f"`{member.name}` is already verified.", ephemeral=True)
        return

    try:
        if unverified_role in member.roles:
            await member.remove_roles(unverified_role)
        await member.add_roles(verified_role)
        await interaction.response.send_message(f"Successfully verified `{member.name}`.", ephemeral=True)
        await log_event(interaction.guild, f"Bypass: `{member.name}` was manually verified by `{interaction.user.name}`.")
    except nextcord.Forbidden:
        await interaction.response.send_message("I don't have the permissions to manage that member's roles.", ephemeral=True)


# --- Bot Events ---
@bot.event
async def on_ready():
    global guild_settings
    guild_settings = load_settings() # Load settings on bot start
    
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')
    for guild_id in guild_settings.keys():
        settings = guild_settings[guild_id]
        if settings.get("verification_type") == "button":
            bot.add_view(VerifyView())
        elif settings.get("verification_type") == "captcha":
            bot.add_view(CaptchaView())

@bot.event
async def on_member_join(member: nextcord.Member):
    guild = member.guild
    settings = guild_settings.get(guild.id)
    if not settings:
        return

    unverified_role = guild.get_role(settings.get("unverified_role"))
    verification_channel = guild.get_channel(settings.get("verification_channel"))

    account_age = datetime.datetime.utcnow() - member.created_at
    if account_age.days < 7:
        await log_event(guild, f"⚠️ **Possible Alt Account:** `{member.name}` joined. Account age is only {account_age.days} days.")

    recent_joins.append(datetime.datetime.utcnow())
    time_limit = datetime.timedelta(seconds=60)
    recent_joins[:] = [join_time for join_time in recent_joins if datetime.datetime.utcnow() - join_time < time_limit]
    
    raid_threshold = 5
    if len(recent_joins) > raid_threshold:
        await log_event(guild, f"🚨 **Potential Raid Detected:** `{len(recent_joins)}` users have joined in the last minute. The server may be under attack.")

    if unverified_role and verification_channel:
        try:
            await member.add_roles(unverified_role)
            await verification_channel.send(f"Welcome, {member.mention}! Please verify yourself in this channel.", delete_after=30)
            member_join_times[member.id] = datetime.datetime.utcnow()
            await log_event(guild, f"➕ `{member.name}` joined and was given the unverified role.")

            if settings.get("kick_timer", 0) > 0:
                await asyncio.sleep(settings["kick_timer"] * 60)
                if unverified_role in member.roles:
                    try:
                        await member.kick(reason="Failed to verify in time.")
                        await log_event(guild, f"👢 `{member.name}` was kicked for failing to verify within {settings['kick_timer']} minutes.")
                    except nextcord.Forbidden:
                        await log_event(guild, f"❌ Failed to kick `{member.name}`. Missing permissions.")
        except nextcord.Forbidden:
            await log_event(guild, f"❌ Could not add unverified role to `{member.name}`. Check bot permissions.")
    else:
        await log_event(guild, "❌ Verification roles or channel not found. Check your setup.")

@bot.event
async def on_guild_channel_delete(channel):
    guild = channel.guild
    await log_event(guild, f"‼️ **Anti-Nuke Warning:** Channel `{channel.name}` was just deleted. Monitoring for further suspicious activity.")

@bot.event
async def on_message(message: nextcord.Message):
    user = message.author
    if user.bot:
        return

    if user.id in captcha_codes and message.channel.type == nextcord.ChannelType.private:
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            await user.send("Verification failed: The server is not configured correctly.")
            del captcha_codes[user.id]
            return

        if message.content.upper() == captcha_codes[user.id].upper():
            try:
                member = guild.get_member(user.id)
                if not member:
                    await user.send("Verification failed: You are no longer a member of the server.")
                    del captcha_codes[user.id]
                    return

                settings = guild_settings.get(guild.id)
                verified_role = guild.get_role(settings.get("verified_role"))
                unverified_role = guild.get_role(settings.get("unverified_role"))
                
                await member.remove_roles(unverified_role)
                await member.add_roles(verified_role)
                
                await user.send("Verification successful! You now have access to the server.")
                await log_event(guild, f"✅ `{user.name}` successfully passed the captcha.")
                
            except nextcord.Forbidden:
                await user.send("Verification failed: I don't have the permissions to manage your roles. Please contact a server admin.")
            finally:
                if user.id in captcha_codes:
                    del captcha_codes[user.id]
        else:
            try:
                await user.send("Incorrect code. Please try the verification process again by clicking the button in the verification channel.")
                await log_event(guild, f"❌ `{user.name}` failed the captcha.")
            except nextcord.Forbidden:
                pass
            finally:
                if user.id in captcha_codes:
                    del captcha_codes[user.id]

    await bot.process_commands(message)

bot.run(DISCORD_TOKEN)

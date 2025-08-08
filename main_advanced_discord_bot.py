import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
PREFIX = os.getenv('BOT_PREFIX', '!')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

@bot.event
async def on_ready():
    print(f'Bot connected as {bot.user}')

@bot.command()
async def ping(ctx):
    """Responds with pong."""
    await ctx.send('pong!')

@bot.command()
async def say(ctx, *, message: str):
    """Repeats your message."""
    await ctx.send(message)

@bot.command()
@commands.has_permissions(administrator=True)
async def clear(ctx, amount: int = 5):
    """Clears a number of messages."""
    await ctx.channel.purge(limit=amount+1)
    await ctx.send(f'Cleared {amount} messages.', delete_after=3)

@bot.command()
async def info(ctx):
    """Shows bot info."""
    embed = discord.Embed(
        title="Advanced Discord Bot",
        description="A multipurpose bot for server management and fun!",
        color=discord.Color.blue()
    )
    embed.add_field(name="Prefix", value=PREFIX)
    embed.add_field(name="Commands", value="ping, say, clear, info")
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('Missing arguments. Please check the command usage.')
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send('You do not have permission to use this command.')
    else:
        await ctx.send(f'Error: {str(error)}')

if __name__ == "__main__":
    bot.run(TOKEN)
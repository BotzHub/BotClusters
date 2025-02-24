import os
import subprocess
import json
import time
import logging
import signal
from concurrent.futures import ThreadPoolExecutor
import shutil
import argparse
import random
from pathlib import Path
from phrase import WORD_LIST
from logging.handlers import RotatingFileHandler
import threading
import re
from dotenv import load_dotenv
import asyncio

LOG_FILE = 'bot_manager.log'
LOG_SIZE = 10 * 1024 * 1024  # 10 MB
LOG_BACKUP_COUNT = 5
SUPERVISORD_CONF_DIR = "/etc/supervisor/conf.d"

logging.basicConfig(
    handlers=[RotatingFileHandler(LOG_FILE, maxBytes=LOG_SIZE, backupCount=LOG_BACKUP_COUNT)],
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

bot_lock = threading.Lock()

def generate_prefix():
    """Generate a random prefix for bot naming."""
    word1 = random.choice(WORD_LIST)
    word2 = random.choice(WORD_LIST)
    prefix = f"{word1} {word2}"
    logging.info(f'Generated prefix: {prefix}')
    return prefix

def validate_config(clusters):
    """Ensure config is valid before starting bots."""
    required_keys = ['bot_number', 'git_url', 'branch', 'run_command']
    seen_bot_suffixes = set()
    bot_suffix_pattern = re.compile(r'bot\d+$')

    for cluster in clusters:
        if not all(key in cluster for key in required_keys):
            logging.error(f"Missing required fields in: {cluster.get('name', 'Unknown')}")
            return False

        if not cluster['git_url'].startswith('http'):
            logging.error(f"Invalid git_url for {cluster['name']}.")
            return False

        match = bot_suffix_pattern.search(cluster['bot_number'])
        if not match:
            logging.error(f"Invalid bot_number format for {cluster['name']}: {cluster['bot_number']}")
            return False

        bot_suffix = match.group()
        if bot_suffix in seen_bot_suffixes:
            logging.error(f"Duplicate bot suffix found: {bot_suffix} in {cluster['bot_number']}")
            return False

        seen_bot_suffixes.add(bot_suffix)

    logging.info("Configuration validation successful.")
    return True

def load_config(file_path):
    """Load bot configurations from a JSON file."""
    logging.info(f'Loading configuration from {file_path}')
    
    try:
        with open(file_path, "r") as jsonfile:
            config = json.load(jsonfile)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logging.error(f"Error loading JSON file: {e}")
        return []

    clusters = []
    for cluster in config.get('clusters', []):
        details_str = os.getenv(cluster['name'], '{}')
        
        try:
            details = json.loads(details_str)
            if not isinstance(details, list) or len(details) < 4:
                logging.warning(f"Skipping cluster {cluster['name']} due to missing details.")
                continue

            prefix = generate_prefix()
            cluster_name = f"{prefix} {cluster['name']}"

            clusters.append({
                "name": cluster_name,
                "bot_number": f"{prefix} {details[0]}",
                "git_url": details[1],
                "branch": details[2],
                "run_command": details[3],
                "env": details[4] if len(details) > 4 and isinstance(details[4], dict) else {}
            })

        except json.JSONDecodeError:
            logging.error(f"Error decoding JSON for {cluster['name']}, skipping.")
            continue

    if not validate_config(clusters):
        raise ValueError("Invalid configuration file.")

    return clusters

load_dotenv('cluster.env', override=True)
clusters = load_config("config.json")

def write_supervisord_config(cluster, command):
    """Write a supervisord config for the bot."""
    config_path = Path(SUPERVISORD_CONF_DIR) / f"{cluster['bot_number'].replace(' ', '_')}.conf"
    logging.info(f"Writing supervisord configuration for {cluster['bot_number']} at {config_path}")

    env_vars = ','.join([f'{key}="{value}"' for key, value in cluster['env'].items()]) if cluster['env'] else ""

    config_content = f"""
    [program:{cluster['bot_number'].replace(' ', '_')}]
    command={command}
    directory=/app/{cluster['bot_number'].replace(' ', '_')}
    autostart=true
    autorestart=true
    startretries=5
    stderr_logfile=/var/log/supervisor/{cluster['bot_number'].replace(' ', '_')}_err.log
    stdout_logfile=/var/log/supervisor/{cluster['bot_number'].replace(' ', '_')}_out.log
    {f"environment={env_vars}" if env_vars else ""}
    """

    config_path.write_text(config_content.strip())
    logging.info(f"Supervisord configuration for {cluster['bot_number']} written successfully.")

async def start_bot(cluster):
    """Clone, set up, and start a bot."""
    with bot_lock:
        logging.info(f'Starting bot: {cluster["bot_number"]}')
        bot_env = os.environ.copy()
        bot_env.update(cluster.get('env', {}))
        bot_dir = Path('/app') / cluster['bot_number'].replace(" ", "_")
        venv_dir = bot_dir / 'venv'
        requirements_file = bot_dir / 'requirements.txt'
        bot_file = bot_dir / cluster['run_command']
        branch = cluster.get('branch', 'main')
        log_dir = Path('/var/log/bots') / cluster['bot_number'].replace(" ", "_")

        try:
            if bot_dir.exists():
                logging.info(f'Removing existing directory: {bot_dir}')
                shutil.rmtree(bot_dir)

            logging.info(f'Cloning {cluster["bot_number"]} from {cluster["git_url"]} (branch: {branch})')
            subprocess.run(['git', 'clone', '-b', branch, '--single-branch', cluster['git_url'], str(bot_dir)], check=True)

            if requirements_file.exists():
                logging.info(f'Installing requirements for {cluster["bot_number"]}')
                # Create virtual environment
                logging.info(f'Creating virtual environment for {cluster["bot_number"]}')
                subprocess.run(['python3', '-m', 'venv', str(venv_dir)], check=True)
                pip_command = [str(venv_dir / 'bin' / 'pip'), 'install', '--no-cache-dir', '-r', str(requirements_file)]
                subprocess.run(pip_command, check=True)

            if bot_file.suffix == ".sh":
                command = f"bash {bot_file}"
            elif bot_file.suffix == ".py":
                command = f"{venv_dir / 'bin' / 'python3'} {bot_file}"
            else:
                command = f"{venv_dir / 'bin' / 'python3'} -m {bot_file.stem}"

            write_supervisord_config(cluster, command)
            await reload_supervisord()
            logging.info(f"{cluster['bot_number']} started successfully via supervisord.")

        except subprocess.CalledProcessError as e:
            logging.error(f"Error while processing {cluster['bot_number']}: {e}")

async def sort_bot_run_commands(clusters):
    """Sort and start bots based on their run command types."""
    for cluster in clusters:
        run_command = cluster['run_command']
        if not run_command.endswith('.sh'):
            logging.info(f"Setting up isolated venv environment for {cluster['bot_number']}")
            await start_bot(cluster)
        else:
            logging.info(f"Installing normally for {cluster['bot_number']}")
            await start_bot(cluster)

async def async_supervisorctl(command):
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logging.error(f"Supervisorctl command failed: {stderr.decode()}")
    else:
        logging.info(f"Supervisorctl command succeeded: {stdout.decode()}")

async def reload_supervisord():
    """Reload and update supervisord after modifying configurations."""
    logging.info("Reloading supervisord...")
    await async_supervisorctl("supervisorctl reread")
    await async_supervisorctl("supervisorctl update")
    logging.info("Supervisord updated successfully.")

async def stop_bot(bot_number):
    """Stop and remove a bot's supervisord configuration."""
    logging.info(f"Stopping bot: {bot_number}")
    bot_conf_name = bot_number.replace(" ", "_")
    
    try:
        subprocess.run(["supervisorctl", "stop", bot_conf_name], check=False)
        time.sleep(5)  # Wait for the bot to shutdown gracefully
    except subprocess.CalledProcessError:
        logging.error(f"Failed to stop bot {bot_number}")

    conf_path = Path(SUPERVISORD_CONF_DIR) / f"{bot_conf_name}.conf"
    if conf_path.exists():
        conf_path.unlink()
        logging.info(f"Removed supervisord configuration for {bot_number}.")
    await reload_supervisord()

async def restart_all_bots():
    """Restart all bots managed by the system."""
    logging.info('Restarting all bots...')
    for cluster in clusters:
        await stop_bot(cluster['bot_number'])
    await reload_supervisord()

def signal_handler(sig, frame):
    logging.info('Shutting down...')
    asyncio.run(restart_all_bots())
    exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

async def main_async():
    parser = argparse.ArgumentParser(description='Bot Manager')
    parser.add_argument('--restart', action='store_true', help='Restart all bots')
    args = parser.parse_args()

    if args.restart:
        logging.info('Restarting bot manager...')
        await asyncio.gather(*(async_supervisorctl(f"supervisorctl stop {cluster['bot_number'].replace(' ', '_')}") for cluster in clusters))
        await reload_supervisord()
    else:
        logging.info('Starting bot manager...')
        await sort_bot_run_commands(clusters)

if __name__ == "__main__":
    asyncio.run(main_async())

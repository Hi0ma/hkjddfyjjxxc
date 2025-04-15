import os
import sys
import json
import asyncio
import argparse
import re
import time
import random
import csv
from typing import List, Tuple, Dict, Optional
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    UsernameInvalidError,
    FloodWaitError,
    SessionExpiredError,
    AuthKeyDuplicatedError,
)
from telethon.tl.functions.account import CheckUsernameRequest, UpdateUsernameRequest
from telethon.tl.functions.messages import SendMessageRequest
from telethon.tl.functions.channels import UpdateUsernameRequest as ChannelUpdateUsernameRequest
from colorama import init, Fore, Style
from tabulate import tabulate
import tqdm
import logging
import aiosqlite
from aiohttp import ClientSession, ClientError, TCPConnector
import subprocess

init(autoreset=True)

logging.basicConfig(
    filename="telegram_script.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger()

USERNAME_CACHE: Dict[str, Tuple[bool, str]] = {}

PROGRESS_FILE = "progress.json"
AVAILABLE_FILE = "available_usernames.txt"
PENDING_FILE = "pending_usernames.txt"
REPORT_FILE = "username_report.txt"
CSV_REPORT_FILE = "username_report.csv"
PROXY_FILE = "proxy.txt"
CONFIG_FILE = "config.json"

if sys.version_info < (3, 7):
    print(f"{Fore.RED}{Style.BRIGHT}Python 3.7+ required!{Style.RESET_ALL}")
    sys.exit(1)

class AccountManager:
    def __init__(self):
        self.sessions_db = "sessions.db"
        self.db_lock = asyncio.Lock()

    async def init_db(self):
        async with aiosqlite.connect(self.sessions_db) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    phone TEXT PRIMARY KEY,
                    session TEXT,
                    api_id TEXT,
                    api_hash TEXT,
                    proxy TEXT
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS account_state (
                    phone TEXT PRIMARY KEY,
                    request_count INTEGER,
                    last_request_time REAL,
                    flood_wait_until REAL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS checked_usernames (
                    username TEXT PRIMARY KEY,
                    available INTEGER,
                    reason TEXT,
                    check_time REAL
                )
            """)
            await conn.commit()
        logger.info("Database initialized")

    async def save_session(self, phone: str, session_string: str, api_id: str, api_hash: str, proxy: str = ""):
        async with self.db_lock:
            async with aiosqlite.connect(self.sessions_db) as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO sessions (phone, session, api_id, api_hash, proxy) VALUES (?, ?, ?, ?, ?)",
                    (phone, session_string, api_id, api_hash, proxy)
                )
                await conn.commit()
        logger.info(f"Session saved for {mask_phone(phone)}")

    async def load_sessions(self) -> Dict[str, Dict[str, str]]:
        async with self.db_lock:
            async with aiosqlite.connect(self.sessions_db) as conn:
                cursor = await conn.execute("SELECT phone, session, api_id, api_hash, proxy FROM sessions")
                rows = await cursor.fetchall()
                sessions = {
                    row[0]: {
                        "session": row[1],
                        "api_id": row[2],
                        "api_hash": row[3],
                        "proxy": row[4] if row[4] else None
                    }
                    for row in rows
                }
                return sessions

    async def delete_session(self, phone: str):
        async with self.db_lock:
            async with aiosqlite.connect(self.sessions_db) as conn:
                await conn.execute("DELETE FROM sessions WHERE phone = ?", (phone,))
                await conn.execute("DELETE FROM account_state WHERE phone = ?", (phone,))
                await conn.commit()
        logger.info(f"Session deleted for {mask_phone(phone)}")

    async def update_account_state(self, phone: str, request_count: int, flood_wait_until: float = 0):
        async with self.db_lock:
            async with aiosqlite.connect(self.sessions_db) as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO account_state (phone, request_count, last_request_time, flood_wait_until) VALUES (?, ?, ?, ?)",
                    (phone, request_count, time.time(), flood_wait_until)
                )
                await conn.commit()

    async def is_account_available(self, phone: str) -> bool:
        async with self.db_lock:
            async with aiosqlite.connect(self.sessions_db) as conn:
                cursor = await conn.execute("SELECT flood_wait_until FROM account_state WHERE phone = ?", (phone,))
                result = await cursor.fetchone()
                return not result or result[0] <= time.time()

    async def get_flood_wait_time(self, phone: str) -> float:
        async with self.db_lock:
            async with aiosqlite.connect(self.sessions_db) as conn:
                cursor = await conn.execute("SELECT flood_wait_until FROM account_state WHERE phone = ?", (phone,))
                result = await cursor.fetchone()
                if result and result[0] > time.time():
                    return result[0] - time.time()
                return 0

    async def save_checked_username(self, username: str, available: bool, reason: str):
        async with self.db_lock:
            async with aiosqlite.connect(self.sessions_db) as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO checked_usernames (username, available, reason, check_time) VALUES (?, ?, ?, ?)",
                    (username.lower(), int(available), reason, time.time())
                )
                await conn.commit()

    async def is_username_checked(self, username: str) -> Optional[Tuple[bool, str]]:
        async with self.db_lock:
            async with aiosqlite.connect(self.sessions_db) as conn:
                cursor = await conn.execute("SELECT available, reason FROM checked_usernames WHERE username = ?", (username.lower(),))
                result = await cursor.fetchone()
                return (bool(result[0]), result[1]) if result else None

class ProxyManager:
    def __init__(self):
        self.proxies = []
        self.proxy_status = {}
        self.lock = asyncio.Lock()
        self.load_proxies()

    def load_proxies(self):
        try:
            if os.path.exists(PROXY_FILE):
                with open(PROXY_FILE, 'r') as f:
                    self.proxies = [line.strip() for line in f if line.strip()]
                self.proxy_status = {proxy: {"healthy": True, "last_checked": 0, "failures": 0} for proxy in self.proxies}
                logger.info(f"Loaded {len(self.proxies)} proxies")
            else:
                logger.warning(f"{PROXY_FILE} not found")
        except Exception as e:
            logger.error(f"Failed to load proxies: {str(e)}")
            print_colored(f"Failed to load proxies: {str(e)}", Fore.RED)

    async def check_proxy_health(self, proxy: str, timeout: float = 3.0) -> bool:
        try:
            proxy_parts = proxy.split(':')
            if len(proxy_parts) < 2:
                logger.warning(f"Invalid proxy format: {proxy}")
                return False
            host, port = proxy_parts[0], int(proxy_parts[1])
            async with ClientSession(connector=TCPConnector(limit=1)) as session:
                async with session.get("https://api.telegram.org", timeout=timeout, proxy=f"http://{host}:{port}") as response:
                    return response.status == 200
        except Exception as e:
            logger.warning(f"Proxy {proxy} health check failed: {str(e)}")
            return False

    async def get_proxy(self) -> Optional[Tuple[str, int, Optional[str]]]:
        async with self.lock:
            current_time = time.time()
            healthy_proxies = [
                proxy for proxy, status in self.proxy_status.items()
                if status["healthy"] and status["failures"] < 3
            ]
            if not healthy_proxies:
                logger.warning("No healthy proxies")
                return None

            proxy = random.choice(healthy_proxies)
            status = self.proxy_status[proxy]

            if current_time - status["last_checked"] > 180:
                status["healthy"] = await self.check_proxy_health(proxy)
                status["last_checked"] = current_time
                if not status["healthy"]:
                    status["failures"] += 1
                    logger.warning(f"Proxy {proxy} unhealthy, failures: {status['failures']}")
                    return await self.get_proxy()

            try:
                parts = proxy.split(':')
                if len(parts) == 2:
                    return parts[0], int(parts[1]), None
                elif len(parts) >= 3:
                    return parts[0], int(parts[1]), parts[2]
                logger.error(f"Invalid proxy format {proxy}")
                status["healthy"] = False
                status["failures"] += 1
                return await self.get_proxy()
            except Exception as e:
                logger.error(f"Error parsing proxy {proxy}: {str(e)}")
                status["healthy"] = False
                status["failures"] += 1
                return await self.get_proxy()

    async def mark_proxy_failure(self, proxy: str):
        if not proxy:
            return
        async with self.lock:
            if proxy in self.proxy_status:
                self.proxy_status[proxy]["failures"] += 1
                self.proxy_status[proxy]["healthy"] = False
                logger.warning(f"Proxy {proxy} failure count: {self.proxy_status[proxy]['failures']}")
                if self.proxy_status[proxy]["failures"] >= 3:
                    logger.error(f"Proxy {proxy} disabled")

def print_colored(text: str, color: str):
    print(f"{color}{Style.BRIGHT}{text}{Style.RESET_ALL}")
    logger.info(text)

def print_header(title: str):
    print_colored(f"[{title}]", Fore.CYAN)

def print_footer():
    print_colored("=", Fore.CYAN)

def mask_phone(phone: str) -> str:
    return phone[:-4] + "****" if len(phone) >= 4 else phone

def display_welcome():
    print_colored("Telegram Username Checker", Fore.MAGENTA)
    print_colored("Use dedicated accounts to avoid bans!", Fore.RED)
    print_footer()

def load_config() -> Dict:
    default_config = {"api_id": "", "api_hash": "", "default_phone": ""}
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        with open(CONFIG_FILE, "w") as f:
            json.dump(default_config, f, indent=4)
        print_colored(f"Created {CONFIG_FILE}. Please fill it.", Fore.GREEN)
        return default_config
    except Exception as e:
        print_colored(f"Error handling {CONFIG_FILE}: {str(e)}", Fore.RED)
        return default_config

def validate_username(username: str) -> Tuple[bool, str]:
    clean_username = username.replace("@", "").strip()
    if not clean_username:
        return False, "Empty username"
    if len(clean_username) < 5:
        return False, "Username too short (<5)"
    if len(clean_username) > 32:
        return False, "Username too long (>32)"
    if not re.match(r'^[A-Za-z][A-Za-z0-9_]*$', clean_username):
        return False, "Invalid characters"
    return True, clean_username

def load_all_usernames() -> List[str]:
    try:
        usernames = set()
        if os.path.exists("all_usernames.txt"):
            with open("all_usernames.txt", "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        usernames.update(u.strip() for u in line.split(",") if u.strip())
            usernames = list(usernames)
            print_colored(f"Loaded {len(usernames)} usernames", Fore.GREEN)
            logger.info(f"Loaded {len(usernames)} usernames")
            return usernames
        print_colored("all_usernames.txt not found", Fore.RED)
        return []
    except Exception as e:
        print_colored(f"Failed to load usernames: {str(e)}", Fore.RED)
        logger.error(f"Failed to load usernames: {str(e)}")
        return []

def save_progress(last_checked: str, success: List[str], failed: List[Tuple[str, str]], pending: List[str]):
    progress = {
        "last_checked": last_checked,
        "success": success,
        "failed": failed,
        "pending": pending
    }
    try:
        with open(PROGRESS_FILE, "w") as f:
            json.dump(progress, f, indent=4)
        with open(PENDING_FILE, "w") as f:
            for u in pending:
                f.write(f"{u}\n")
        logger.info(f"Progress saved: @{last_checked}, {len(pending)} pending")
    except Exception as e:
        logger.error(f"Failed to save progress: {str(e)}")
        print_colored(f"Failed to save progress: {str(e)}", Fore.RED)

def load_progress() -> Tuple[str, List[str], List[Tuple[str, str]], List[str]]:
    try:
        if not os.path.exists(PROGRESS_FILE):
            return "", [], [], []
        with open(PROGRESS_FILE, "r") as f:
            progress = json.load(f)
        return (
            progress.get("last_checked", ""),
            progress.get("success", []),
            [(str(u), str(r)) for u, r in progress.get("failed", [])],
            progress.get("pending", [])
        )
    except Exception as e:
        logger.error(f"Failed to load progress: {str(e)}")
        print_colored(f"Failed to load progress: {str(e)}", Fore.RED)
        return "", [], [], []

def save_available_username(username: str):
    try:
        with open(AVAILABLE_FILE, "a") as f:
            f.write(f"@{username}\n")
        logger.info(f"Saved @{username}")
    except Exception as e:
        logger.error(f"Failed to save @{username}: {str(e)}")
        print_colored(f"Failed to save @{username}: {str(e)}", Fore.RED)

def save_report(success: List[str], failed: List[Tuple[str, str]], pending: List[str], accounts_used: List[str]):
    try:
        with open(REPORT_FILE, "w") as f:
            f.write(f"Telegram Username Checker Report\n")
            f.write(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Accounts: {', '.join(mask_phone(p) for p in accounts_used)}\n")
            f.write(f"Total Checked: {len(success) + len(failed)}\n")
            f.write(f"Available: {len(success)}\n")
            f.write(f"Not Available: {len(failed)}\n")
            f.write(f"Pending: {len(pending)}\n\n")
            if success:
                f.write("Available:\n")
                for u in success:
                    f.write(f"  @{u}\n")
            if failed:
                f.write("\nNot Available:\n")
                for u, r in failed:
                    f.write(f"  @{u}: {r}\n")
            if pending:
                f.write("\nPending:\n")
                for u in pending:
                    f.write(f"  @{u}\n")

        with open(CSV_REPORT_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Username", "Status", "Reason"])
            for u in success:
                writer.writerow([f"@{u}", "Available", ""])
            for u, r in failed:
                writer.writerow([f"@{u}", "Not Available", r])
            for u in pending:
                writer.writerow([f"@{u}", "Pending", "Not checked"])

        print_colored(f"Report saved to {REPORT_FILE} and {CSV_REPORT_FILE}", Fore.GREEN)
        logger.info(f"Report saved")
    except Exception as e:
        print_colored(f"Failed to save report: {str(e)}", Fore.RED)
        logger.error(f"Failed to save report: {str(e)}")

async def check_internet_connection() -> bool:
    try:
        async with ClientSession() as session:
            async with session.get("https://api.telegram.org", timeout=5) as response:
                return response.status == 200
    except Exception as e:
        logger.warning(f"Internet check failed: {str(e)}")
        return False

async def authenticate_client(client: TelegramClient, phone: str, proxy_manager: ProxyManager) -> bool:
    if not await check_internet_connection():
        print_colored("No internet connection", Fore.RED)
        return False

    max_retries = 3
    for attempt in range(max_retries):
        try:
            if not await client.is_user_authorized():
                print_colored("Sending code...", Fore.BLUE)
                await client.send_code_request(phone)
                for retry in range(2):
                    code = input(f"{Fore.BLUE}Enter code: {Style.RESET_ALL} ")
                    try:
                        await client.sign_in(phone, code)
                        return True
                    except SessionPasswordNeededError:
                        pw = input(f"{Fore.BLUE}Enter 2FA password: {Style.RESET_ALL} ")
                        await client.sign_in(password=pw)
                        return True
                    except Exception as e:
                        print_colored(f"Invalid code: {str(e)} ({retry + 1}/2)", Fore.RED)
                        if retry < 1:
                            print_colored("Resending code...", Fore.BLUE)
                            await client.send_code_request(phone, force_sms=True)
                print_colored("Too many invalid code attempts", Fore.RED)
                return False
            return True
        except AuthKeyDuplicatedError:
            print_colored(f"Session for {mask_phone(phone)} invalid", Fore.RED)
            return False
        except ClientError as e:
            logger.warning(f"Network error for {mask_phone(phone)}: {str(e)}, attempt {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2)
                continue
            print_colored(f"Login failed: {str(e)}", Fore.RED)
            return False
        except Exception as e:
            print_colored(f"Login failed: {str(e)}", Fore.RED)
            logger.error(f"Login failed for {mask_phone(phone)}: {str(e)}")
            return False
    return False

async def check_username_available(client: TelegramClient, username: str, phone: str, account_manager: AccountManager, proxy_manager: ProxyManager, max_retries: int = 3) -> Tuple[bool, Optional[str]]:
    cache_key = username.lower()
    if cache_key in USERNAME_CACHE:
        logger.info(f"Cached result for @{username}")
        return USERNAME_CACHE[cache_key]

    checked_result = await account_manager.is_username_checked(cache_key)
    if checked_result:
        logger.info(f"Database result for @{username}")
        USERNAME_CACHE[cache_key] = checked_result
        return checked_result

    sessions = await account_manager.load_sessions()
    proxy_str = sessions.get(phone, {}).get("proxy", "direct")

    for attempt in range(max_retries):
        try:
            print_colored(f"Checking @{username} via {proxy_str}...", Fore.BLUE)
            result = await client(CheckUsernameRequest(username=username))
            USERNAME_CACHE[cache_key] = (result, None)
            await account_manager.save_checked_username(cache_key, result, "Available" if result else "Taken")
            print_colored(f"@{username} {'Available' if result else 'Taken'}", Fore.GREEN if result else Fore.RED)
            return result, None
        except UsernameInvalidError:
            print_colored(f"@{username} Invalid", Fore.RED)
            USERNAME_CACHE[cache_key] = (False, "Invalid username")
            await account_manager.save_checked_username(cache_key, False, "Invalid username")
            return False, "Invalid username"
        except FloodWaitError as e:
            wait_time = e.seconds + random.uniform(10, 20)
            print_colored(f"@{username} Flood limit ({wait_time:.1f}s)", Fore.YELLOW)
            logger.warning(f"Flood limit for @{username}: {wait_time}s")
            await account_manager.update_account_state(phone, 0, time.time() + wait_time)
            USERNAME_CACHE[cache_key] = (False, f"Flood limit ({wait_time}s)")
            await account_manager.save_checked_username(cache_key, False, f"Flood limit ({wait_time}s)")
            return False, f"Flood limit ({wait_time}s)"
        except SessionExpiredError:
            print_colored(f"Session expired for {mask_phone(phone)}", Fore.RED)
            await account_manager.delete_session(phone)
            USERNAME_CACHE[cache_key] = (False, "Session expired")
            await account_manager.save_checked_username(cache_key, False, "Session expired")
            return False, "Session expired"
        except ClientError as e:
            await proxy_manager.mark_proxy_failure(proxy_str)
            if attempt < max_retries - 1:
                logger.warning(f"Network error for @{username}: {str(e)}, attempt {attempt + 1}/{max_retries}")
                await asyncio.sleep(2)
                continue
            print_colored(f"@{username} Network error: {str(e)}", Fore.RED)
            USERNAME_CACHE[cache_key] = (False, f"Network error: {str(e)}")
            await account_manager.save_checked_username(cache_key, False, f"Network error: {str(e)}")
            return False, f"Network error: {str(e)}"
        except Exception as e:
            print_colored(f"@{username} Error: {str(e)}", Fore.RED)
            logger.error(f"Error checking @{username}: {str(e)}")
            USERNAME_CACHE[cache_key] = (False, str(e))
            await account_manager.save_checked_username(cache_key, False, str(e))
            return False, str(e)
    return False, "Max retries reached"

async def hunting_mode(clients: Dict[str, TelegramClient], account_manager: AccountManager, proxy_manager: ProxyManager):
    print_header("Hunting Mode")
    target_username = input(f"{Fore.BLUE}Enter username to hunt: {Style.RESET_ALL} ").replace("@", "").strip()
    channel_handle = input(f"{Fore.BLUE}Enter channel handle: {Style.RESET_ALL} ").replace("@", "").strip()

    is_valid, reason = validate_username(target_username)
    if not is_valid:
        print_colored(f"Invalid username: {reason}", Fore.RED)
        return

    check_interval = 5
    max_attempts = 20
    claimed = False

    print_colored(f"Hunting @{target_username} for @{channel_handle}...", Fore.CYAN)

    async def try_claim_username(phone: str, client: TelegramClient, username: str, channel: str) -> Tuple[bool, str]:
        try:
            await client(ChannelUpdateUsernameRequest(channel=channel, username=username))
            print_colored(f"@{username} CLAIMED on @{channel}!", Fore.GREEN)
            await client(SendMessageRequest(peer='me', message=f"Claimed @{username} on @{channel}"))
            save_available_username(username)
            return True, ""
        except Exception as e:
            error_msg = str(e)
            if "Flood limit" in error_msg or isinstance(e, FloodWaitError):
                wait_time = getattr(e, 'seconds', 60) + random.uniform(5, 10)
                await account_manager.update_account_state(phone, 0, time.time() + wait_time)
                return False, f"Flood limit ({wait_time}s)"
            try:
                await client(UpdateUsernameRequest(username=username))
                print_colored(f"@{username} CLAIMED on account {mask_phone(phone)}!", Fore.GREEN)
                await client(SendMessageRequest(peer='me', message=f"Claimed @{username} on account"))
                save_available_username(username)
                return True, ""
            except Exception as e2:
                error_msg = str(e2)
                if "Flood limit" in error_msg or isinstance(e2, FloodWaitError):
                    wait_time = getattr(e2, 'seconds', 60) + random.uniform(5, 10)
                    await account_manager.update_account_state(phone, 0, time.time() + wait_time)
                return False, error_msg

    while not claimed:
        try:
            available_accounts = [
                (phone, client) for phone, client in clients.items()
                if await account_manager.is_account_available(phone)
            ]
            if not available_accounts:
                print_colored("No available accounts! Waiting...", Fore.RED)
                await asyncio.sleep(10)
                continue

            tasks = []
            sessions = await account_manager.load_sessions()
            for phone, client in available_accounts:
                proxy_data = await proxy_manager.get_proxy()
                if proxy_data:
                    proxy_str = f"{proxy_data[0]}:{proxy_data[1]}" + (f":{proxy_data[2]}" if proxy_data[2] else "")
                    client_kwargs = {
                        "session": client.session,
                        "api_id": client.api_id,
                        "api_hash": client.api_hash,
                        "proxy": ("socks5", proxy_data[0], proxy_data[1]) if proxy_data else None
                    }
                    new_client = TelegramClient(**client_kwargs)
                    await new_client.connect()
                    clients[phone] = new_client
                    await account_manager.save_session(phone, new_client.session.save(), str(client.api_id), client.api_hash, proxy_str)
                    print_colored(f"Rotated proxy for {mask_phone(phone)} to {proxy_str}", Fore.YELLOW)

                tasks.append(check_username_available(client, target_username, phone, account_manager, proxy_manager))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for idx, result in enumerate(results):
                if isinstance(result, Exception):
                    phone, _ = available_accounts[idx]
                    print_colored(f"Error with {mask_phone(phone)}: {str(result)}", Fore.RED)
                    continue

                available, error = result
                phone, client = available_accounts[idx]

                if available:
                    print_colored(f"@{target_username} AVAILABLE! Claiming...", Fore.GREEN)
                    success, error_msg = await try_claim_username(phone, client, target_username, channel_handle)
                    if success:
                        claimed = True
                        break

                    for next_phone, next_client in [(p, c) for p, c in available_accounts if p != phone]:
                        if not await account_manager.is_account_available(next_phone):
                            continue
                        print_colored(f"Trying with {mask_phone(next_phone)}...", Fore.YELLOW)
                        success, error_msg = await try_claim_username(next_phone, next_client, target_username, channel_handle)
                        if success:
                            claimed = True
                            break
                        print_colored(f"Failed: {error_msg}", Fore.RED)
                    if claimed:
                        break

                elif error and "Flood limit" in error:
                    wait_time = await account_manager.get_flood_wait_time(phone)
                    print_colored(f"{mask_phone(phone)} flood limit ({wait_time:.1f}s)", Fore.YELLOW)
                else:
                    print_colored(f"@{target_username} still taken...", Fore.YELLOW)

            if not claimed:
                print_colored(f"Checking again in {check_interval}s...", Fore.CYAN)
                await asyncio.sleep(check_interval)

        except KeyboardInterrupt:
            print_colored("Hunting stopped", Fore.RED)
            break
        except Exception as e:
            print_colored(f"Hunting error: {str(e)}", Fore.RED)
            logger.error(f"Hunting error: {str(e)}")
            await asyncio.sleep(2)

async def check_single_username(client: TelegramClient, phone: str, account_manager: AccountManager, proxy_manager: ProxyManager):
    print_header("Check Usernames")
    user_input = input(f"{Fore.BLUE}Enter usernames (comma-separated or per line): {Style.RESET_ALL} ")
    usernames = []
    for line in user_input.splitlines():
        usernames.extend(u.strip() for u in line.split(",") if u.strip())
    usernames = list(set(usernames))

    if not usernames:
        print_colored("No usernames provided!", Fore.RED)
        print_footer()
        return

    if not await account_manager.is_account_available(phone):
        wait_time = await account_manager.get_flood_wait_time(phone)
        hours = wait_time / 3600
        print_colored(f"Account {mask_phone(phone)} banned for {wait_time:.1f}s (~{hours:.1f}h)", Fore.YELLOW)
        print_footer()
        return

    max_requests_per_second = 0.5
    success = []
    failed = []

    with tqdm.tqdm(total=len(usernames), desc="Checking usernames", bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}]") as pbar:
        for username in usernames:
            try:
                is_valid, reason = validate_username(username)
                clean_username = reason if is_valid else username.replace("@", "").strip()

                if not is_valid:
                    print_colored(f"@{clean_username} {reason}", Fore.RED)
                    failed.append((clean_username, reason))
                    pbar.update(1)
                    await asyncio.sleep(1 / max_requests_per_second)
                    continue

                available, error = await check_username_available(client, clean_username, phone, account_manager, proxy_manager)
                if available:
                    success.append(clean_username)
                    save_available_username(clean_username)
                else:
                    failed.append((clean_username, error or "Taken"))
                pbar.update(1)
                await asyncio.sleep(1 / max_requests_per_second)
            except asyncio.CancelledError:
                logger.info(f"Single username check for @{clean_username} cancelled")
                raise

    print_header("Results")
    print_colored(f"Available: {len(success)}", Fore.GREEN)
    if success:
        table = [[f"@{u}"] for u in success]
        print(tabulate(table, headers=["Available Username"], tablefmt="fancy_grid", stralign="center"))
    print_colored(f"Not Available: {len(failed)}", Fore.RED)
    if failed:
        table = [[f"@{u}", r] for u, r in failed]
        print(tabulate(table, headers=["Username", "Reason"], tablefmt="fancy_grid", stralign="center"))
    print_footer()
    save_report(success, failed, [], [phone])

async def process_usernames(client: TelegramClient, phone: str, account_manager: AccountManager, proxy_manager: ProxyManager):
    print_header("Check Usernames from File")
    usernames = load_all_usernames()
    if not usernames:
        print_colored("No usernames to process!", Fore.RED)
        print_footer()
        return [], []

    last_checked, success, failed, pending = load_progress()
    start_index = 0
    if last_checked:
        clean_last_checked = last_checked.replace("@", "").strip()
        for i, username in enumerate(usernames):
            clean_username = username.replace("@", "").strip()
            if clean_username.lower() == clean_last_checked.lower():
                start_index = i + 1
                print_colored(f"Resuming from @{clean_username}", Fore.GREEN)
                break
        else:
            print_colored("Last checked username not found, starting from beginning", Fore.YELLOW)

    usernames = usernames[start_index:]
    if not usernames:
        print_colored("All usernames checked!", Fore.GREEN)
        print_header("Final Results")
        print_colored(f"Available: {len(success)}", Fore.GREEN)
        if success:
            table = [[f"@{u}"] for u in success]
            print(tabulate(table, headers=["Available Username"], tablefmt="fancy_grid", stralign="center"))
        print_colored(f"Not Available: {len(failed)}", Fore.RED)
        if failed:
            table = [[f"@{u}", r] for u, r in failed]
            print(tabulate(table, headers=["Username", "Reason"], tablefmt="fancy_grid", stralign="center"))
        if pending:
            print_colored(f"Pending: {len(pending)}", Fore.YELLOW)
            table = [[f"@{u}"] for u in pending]
            print(tabulate(table, headers=["Pending Username"], tablefmt="fancy_grid", stralign="center"))
        print_footer()
        save_report(success, failed, pending, [phone])
        if os.path.exists(PROGRESS_FILE):
            os.remove(PROGRESS_FILE)
        return success, failed

    max_requests_per_second = 0.5
    request_count = 0
    request_semaphore = asyncio.Semaphore(10)

    with tqdm.tqdm(total=len(usernames), desc="Checking usernames", bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]") as pbar:
        for username in usernames:
            async with request_semaphore:
                try:
                    if not await account_manager.is_account_available(phone):
                        wait_time = await account_manager.get_flood_wait_time(phone)
                        hours = wait_time / 3600
                        print_colored(f"Account {mask_phone(phone)} banned for {wait_time:.1f}s (~{hours:.1f}h)", Fore.YELLOW)
                        save_progress(username, success, failed, pending + usernames[usernames.index(username):])
                        break

                    is_valid, reason = validate_username(username)
                    clean_username = reason if is_valid else username.replace("@", "").strip()

                    if not is_valid:
                        print_colored(f"@{clean_username} {reason}", Fore.RED)
                        failed.append((clean_username, reason))
                        save_progress(clean_username, success, failed, pending)
                        await asyncio.sleep(1 / max_requests_per_second)
                        pbar.update(1)
                        continue

                    available, error = await check_username_available(client, clean_username, phone, account_manager, proxy_manager)
                    if available:
                        success.append(clean_username)
                        save_available_username(clean_username)
                    else:
                        failed.append((clean_username, error or "Taken"))

                    request_count += 1
                    await account_manager.update_account_state(phone, request_count)
                    save_progress(clean_username, success, failed, pending)
                    await asyncio.sleep(1 / max_requests_per_second)
                    pbar.update(1)
                except asyncio.CancelledError:
                    logger.info(f"Username processing for @{clean_username} cancelled")
                    save_progress(clean_username, success, failed, pending)
                    raise

    print_header("Results")
    print_colored(f"Available: {len(success)}", Fore.GREEN)
    if success:
        table = [[f"@{u}"] for u in success]
        print(tabulate(table, headers=["Available Username"], tablefmt="fancy_grid", stralign="center"))
    print_colored(f"Not Available: {len(failed)}", Fore.RED)
    if failed:
        table = [[f"@{u}", r] for u, r in failed]
        print(tabulate(table, headers=["Username", "Reason"], tablefmt="fancy_grid", stralign="center"))
    if pending:
        print_colored(f"Pending: {len(pending)}", Fore.YELLOW)
        table = [[f"@{u}"] for u in pending]
        print(tabulate(table, headers=["Pending Username"], tablefmt="fancy_grid", stralign="center"))
    print_footer()
    save_report(success, failed, pending, [phone])
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)

    return success, failed

async def process_multiple_accounts(clients: Dict[str, TelegramClient], account_manager: AccountManager, proxy_manager: ProxyManager, skip_flooded: bool = False):
    print_header("Multiple Accounts Check")
    usernames = load_all_usernames()
    if not usernames:
        print_colored("No usernames to process!", Fore.RED)
        print_footer()
        return [], []

    last_checked, success, failed, pending = load_progress()
    start_index = 0
    if last_checked:
        clean_last_checked = last_checked.replace("@", "").strip()
        for i, username in enumerate(usernames):
            clean_username = username.replace("@", "").strip()
            if clean_username.lower() == clean_last_checked.lower():
                start_index = i + 1
                print_colored(f"Resuming from @{clean_username}", Fore.GREEN)
                break
        else:
            print_colored("Last checked username not found, starting from beginning", Fore.YELLOW)

    usernames = usernames[start_index:] + pending
    pending = []
    if not usernames:
        print_colored("All usernames checked!", Fore.GREEN)
        print_header("Final Results")
        print_colored(f"Available: {len(success)}", Fore.GREEN)
        if success:
            table = [[f"@{u}"] for u in success]
            print(tabulate(table, headers=["Available Username"], tablefmt="fancy_grid", stralign="center"))
        print_colored(f"Not Available: {len(failed)}", Fore.RED)
        if failed:
            table = [[f"@{u}", r] for u, r in failed]
            print(tabulate(table, headers=["Username", "Reason"], tablefmt="fancy_grid", stralign="center"))
        print_footer()
        save_report(success, failed, pending, list(clients.keys()))
        if os.path.exists(PROGRESS_FILE):
            os.remove(PROGRESS_FILE)
        return success, failed

    print_colored("Verifying accounts...", Fore.BLUE)
    if not await check_internet_connection():
        print_colored("No internet connection!", Fore.RED)
        print_footer()
        return [], []

    available_clients = []
    for phone, client in clients.items():
        if skip_flooded and not await account_manager.is_account_available(phone):
            wait_time = await account_manager.get_flood_wait_time(phone)
            hours = wait_time / 3600
            print_colored(f"Skipping {mask_phone(phone)} (banned for {wait_time:.1f}s ~ {hours:.1f}h)", Fore.YELLOW)
            continue
        if await account_manager.is_account_available(phone):
            available_clients.append((phone, client))
        else:
            wait_time = await account_manager.get_flood_wait_time(phone)
            hours = wait_time / 3600
            print_colored(f"{mask_phone(phone)} banned for {wait_time:.1f}s (~{hours:.1f}h)", Fore.YELLOW)

    if not available_clients:
        print_colored("No available accounts!", Fore.RED)
        print_footer()
        return [], []

    max_requests_per_account = 50
    request_semaphore = asyncio.Semaphore(max_requests_per_account * len(available_clients))
    max_requests_per_second = 0.5
    batch_size = 20
    username_batches = [usernames[i:i + batch_size] for i in range(0, len(usernames), batch_size)]
    request_count = {phone: 0 for phone, _ in available_clients}

    async def check_batch(batch: List[str], phone: str, client: TelegramClient):
        nonlocal pending
        batch_success = []
        batch_failed = []
        batch_pending = []

        if not await account_manager.is_account_available(phone):
            wait_time = await account_manager.get_flood_wait_time(phone)
            hours = wait_time / 3600
            print_colored(f"{mask_phone(phone)} banned for {wait_time:.1f}s (~{hours:.1f}h)", Fore.YELLOW)
            batch_pending.extend(batch)
            return batch_success, batch_failed, batch_pending, False

        for username in batch:
            async with request_semaphore:
                try:
                    is_valid, reason = validate_username(username)
                    clean_username = reason if is_valid else username.replace("@", "").strip()
                    if not is_valid:
                        print_colored(f"@{clean_username} {reason}", Fore.RED)
                        batch_failed.append((clean_username, reason))
                        save_progress(clean_username, success + batch_success, failed + batch_failed, pending + batch_pending)
                        await asyncio.sleep(1 / max_requests_per_second)
                        continue

                    if request_count[phone] >= max_requests_per_account:
                        print_colored(f"{mask_phone(phone)} reached limit ({max_requests_per_account})", Fore.YELLOW)
                        batch_pending.extend(batch[batch.index(username):])
                        return batch_success, batch_failed, batch_pending, False

                    if request_count[phone] >= int(max_requests_per_account * 0.8):
                        print_colored(f"Warning: {mask_phone(phone)} nearing limit ({request_count[phone]}/{max_requests_per_account})", Fore.YELLOW)

                    available, error = await check_username_available(client, clean_username, phone, account_manager, proxy_manager)
                    if available:
                        batch_success.append(clean_username)
                        save_available_username(clean_username)
                    elif error and "Flood limit" in error:
                        batch_pending.extend(batch[batch.index(username):])
                        return batch_success, batch_failed, batch_pending, False
                    else:
                        batch_failed.append((clean_username, error or "Taken"))

                    request_count[phone] += 1
                    await account_manager.update_account_state(phone, request_count[phone])
                    save_progress(clean_username, success + batch_success, failed + batch_failed, pending + batch_pending)
                    await asyncio.sleep(1 / max_requests_per_second)
                except FloodWaitError:
                    wait_time = await account_manager.get_flood_wait_time(phone)
                    hours = wait_time / 3600
                    print_colored(f"{mask_phone(phone)} banned for {wait_time:.1f}s (~{hours:.1f}h)", Fore.YELLOW)
                    batch_pending.extend(batch[batch.index(username):])
                    return batch_success, batch_failed, batch_pending, False
                except asyncio.CancelledError:
                    logger.info(f"Batch check for {mask_phone(phone)} cancelled")
                    save_progress(username, success + batch_success, failed + batch_failed, pending + batch_pending)
                    raise
        return batch_success, batch_failed, batch_pending, True

    with tqdm.tqdm(total=len(usernames), desc="Checking usernames", bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]") as pbar:
        for batch in username_batches:
            tasks = []
            batch_per_client = len(batch) // max(1, len(available_clients))
            for phone, client in available_clients[:]:
                client_batch = batch[:batch_per_client]
                batch = batch[batch_per_client:]
                if client_batch:
                    tasks.append(check_batch(client_batch, phone, client))
            if not tasks:
                print_colored("No available accounts!", Fore.RED)
                save_progress(batch[0] if batch else usernames[-1], success, failed, pending + batch)
                break
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        logger.error(f"Batch error: {str(result)}")
                        continue
                    batch_success, batch_failed, batch_pending, continue_checking = result
                    success.extend(batch_success)
                    failed.extend(batch_failed)
                    pending.extend(batch_pending)
                    if not continue_checking:
                        available_clients = [(p, c) for p, c in available_clients if await account_manager.is_account_available(p)]
                pbar.update(batch_per_client * len(tasks))
            except asyncio.CancelledError:
                logger.info("Batch processing cancelled")
                save_progress(batch[0] if batch else usernames[-1], success, failed, pending)
                raise
            if not available_clients:
                print_colored("No available accounts!", Fore.RED)
                save_progress(batch[0] if batch else usernames[-1], success, failed, pending + batch)
                break

    print_header("Results")
    print_colored(f"Available: {len(success)}", Fore.GREEN)
    if success:
        table = [[f"@{u}"] for u in success]
        print(tabulate(table, headers=["Available Username"], tablefmt="fancy_grid", stralign="center"))
    print_colored(f"Not Available: {len(failed)}", Fore.RED)
    if failed:
        table = [[f"@{u}", r] for u, r in failed]
        print(tabulate(table, headers=["Username", "Reason"], tablefmt="fancy_grid", stralign="center"))
    if pending:
        print_colored(f"Pending: {len(pending)}", Fore.YELLOW)
        table = [[f"@{u}"] for u in pending]
        print(tabulate(table, headers=["Pending Username"], tablefmt="fancy_grid", stralign="center"))
        print_colored(f"Pending usernames saved to {PENDING_FILE}", Fore.BLUE)
    print_footer()
    save_report(success, failed, pending, list(clients.keys()))
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)

    return success, failed

async def main():
    parser = argparse.ArgumentParser(description="Telegram Username Checker")
    parser.add_argument("--phone", help="Phone number")
    parser.add_argument("--skip-flooded", action="store_true", help="Skip flooded accounts")
    args = parser.parse_args()

    config = load_config()
    account_manager = AccountManager()
    proxy_manager = ProxyManager()
    clients: Dict[str, TelegramClient] = {}

    await account_manager.init_db()
    display_welcome()

    try:
        while True:
            sessions = await account_manager.load_sessions()
            phones = list(sessions.keys())

            print_header("Main Menu")
            print_colored("Choose an option:", Fore.WHITE)
            for i, phone in enumerate(phones, 1):
                print_colored(f"[{i}] Account: {mask_phone(phone)}", Fore.WHITE)
            print_colored(f"[{len(phones) + 1}] New Session", Fore.WHITE)
            print_colored(f"[{len(phones) + 2}] Delete Session", Fore.WHITE)
            print_colored(f"[{len(phones) + 3}] Show Banned Accounts", Fore.WHITE)
            if len(phones) > 1:
                print_colored(f"[{len(phones) + 4}] Multiple Accounts Check", Fore.WHITE)
            print_colored(f"[{len(phones) + 5}] Hunting Mode", Fore.WHITE)
            print_footer()

            try:
                max_choice = len(phones) + 5
                choice = int(input(f"{Fore.BLUE}Enter choice: {Style.RESET_ALL} "))

                if 1 <= choice <= len(phones):
                    phone = phones[choice - 1]

                    if not await account_manager.is_account_available(phone):
                        wait_time = await account_manager.get_flood_wait_time(phone)
                        hours = wait_time / 3600
                        print_colored(f"Account {mask_phone(phone)} banned for {wait_time:.1f}s (~{hours:.1f}h)", Fore.YELLOW)
                        continue

                    if phone not in clients:
                        session = sessions[phone]
                        proxy = session.get("proxy")
                        client_kwargs = {
                            "session": StringSession(session["session"]),
                            "api_id": int(session["api_id"]),
                            "api_hash": session["api_hash"]
                        }
                        if proxy and ':' in proxy:
                            proxy_parts = proxy.split(':')
                            if len(proxy_parts) == 2:
                                client_kwargs["proxy"] = ("socks5", proxy_parts[0], int(proxy_parts[1]))
                            elif len(proxy_parts) == 3:
                                client_kwargs["proxy"] = ("socks5", proxy_parts[0], int(proxy_parts[1]), proxy_parts[2])
                        client = TelegramClient(**client_kwargs)
                        clients[phone] = client
                        await client.connect()

                        if not await authenticate_client(client, phone, proxy_manager):
                            await client.disconnect()
                            del clients[phone]
                            print_colored("Failed to authenticate account", Fore.RED)
                            continue

                    while True:
                        print_header("Action Menu")
                        print_colored("Select an action:", Fore.WHITE)
                        print_colored("[1] Check usernames manually", Fore.WHITE)
                        print_colored("[2] Check usernames from file", Fore.WHITE)
                        print_colored("[3] Back to main menu", Fore.WHITE)
                        print_footer()

                        action = input(f"{Fore.BLUE}Enter choice: {Style.RESET_ALL} ")

                        if action == "1":
                            await check_single_username(clients[phone], phone, account_manager, proxy_manager)
                        elif action == "2":
                            await process_usernames(clients[phone], phone, account_manager, proxy_manager)
                        elif action == "3":
                            break

                elif choice == len(phones) + 1:
                    print_header("Create New Session")
                    print_colored("Enter Telegram API credentials", Fore.WHITE)
                    api_id = input(f"{Fore.BLUE}API ID: {Style.RESET_ALL} ") or config.get("api_id", "")
                    api_hash = input(f"{Fore.BLUE}API Hash: {Style.RESET_ALL} ") or config.get("api_hash", "")
                    phone = input(f"{Fore.BLUE}Phone (e.g., +1234567890): {Style.RESET_ALL} ")

                    if not api_id or not api_hash:
                        print_colored("API ID and Hash required!", Fore.RED)
                        continue
                    if not re.match(r'^\+\d+$', phone):
                        print_colored("Invalid phone format!", Fore.RED)
                        continue

                    use_proxy = input(f"{Fore.BLUE}Use proxy? (y/n): {Style.RESET_ALL} ").lower() == 'y'
                    proxy = ""
                    if use_proxy and proxy_manager.proxies:
                        print_colored("Available proxies:", Fore.CYAN)
                        for i, p in enumerate(proxy_manager.proxies[:10], 1):
                            status = proxy_manager.proxy_status.get(p, {})
                            health = "Healthy" if status.get("healthy") else "Unhealthy"
                            print_colored(f"[{i}] {p} {health}", Fore.CYAN)

                        proxy_choice = input(f"{Fore.BLUE}Select proxy (1-{min(10, len(proxy_manager.proxies))} or 'r' for random): {Style.RESET_ALL} ")

                        if proxy_choice.lower() == 'r':
                            proxy_data = await proxy_manager.get_proxy()
                            if proxy_data:
                                proxy = f"{proxy_data[0]}:{proxy_data[1]}" + (f":{proxy_data[2]}" if proxy_data[2] else "")
                        elif proxy_choice.isdigit() and 1 <= int(proxy_choice) <= min(10, len(proxy_manager.proxies)):
                            proxy = proxy_manager.proxies[int(proxy_choice) - 1]
                        else:
                            print_colored("Invalid proxy choice, using random", Fore.RED)
                            proxy_data = await proxy_manager.get_proxy()
                            if proxy_data:
                                proxy = f"{proxy_data[0]}:{proxy_data[1]}" + (f":{proxy_data[2]}" if proxy_data[2] else "")

                        print_colored(f"Selected proxy: {proxy}", Fore.GREEN)

                    client_kwargs = {
                        "session": StringSession(),
                        "api_id": int(api_id),
                        "api_hash": api_hash
                    }
                    if proxy and ':' in proxy:
                        proxy_parts = proxy.split(':')
                        if len(proxy_parts) == 2:
                            client_kwargs["proxy"] = ("socks5", proxy_parts[0], int(proxy_parts[1]))
                        elif len(proxy_parts) == 3:
                            client_kwargs["proxy"] = ("socks5", proxy_parts[0], int(proxy_parts[1]), proxy_parts[2])
                    client = TelegramClient(**client_kwargs)
                    clients[phone] = client
                    await client.connect()

                    if not await authenticate_client(client, phone, proxy_manager):
                        await client.disconnect()
                        del clients[phone]
                        print_colored("Failed to create session", Fore.RED)
                        continue

                    session_string = client.session.save()
                    await account_manager.save_session(phone, session_string, str(client.api_id), client.api_hash, proxy)
                    await account_manager.update_account_state(phone, 0)

                    config["api_id"] = api_id
                    config["api_hash"] = api_hash
                    config["default_phone"] = phone
                    try:
                        with open(CONFIG_FILE, "w") as f:
                            json.dump(config, f, indent=4)
                        print_colored("Updated config.json", Fore.GREEN)
                    except Exception as e:
                        print_colored(f"Failed to update config.json: {str(e)}", Fore.YELLOW)

                    print_colored(f"Session created for {mask_phone(phone)}", Fore.GREEN)

                elif choice == len(phones) + 2:
                    print_header("Delete Session")
                    print_colored("Enter phone number to delete", Fore.WHITE)
                    phone_to_delete = input(f"{Fore.BLUE}Phone: {Style.RESET_ALL} ")
                    if phone_to_delete in sessions:
                        await account_manager.delete_session(phone_to_delete)
                        print_colored(f"Session {mask_phone(phone_to_delete)} deleted", Fore.GREEN)
                    else:
                        print_colored("Phone not found!", Fore.RED)

                elif choice == len(phones) + 3:
                    print_header("Banned Accounts")
                    banned = []
                    for phone in sessions:
                        wait_time = await account_manager.get_flood_wait_time(phone)
                        if wait_time > 0:
                            hours = wait_time / 3600
                            banned.append([mask_phone(phone), f"{wait_time:.1f}s (~{hours:.1f}h)"])
                    if banned:
                        print(tabulate(banned, headers=["Phone", "Time Remaining"], tablefmt="fancy_grid", stralign="center"))
                    else:
                        print_colored("No banned accounts!", Fore.GREEN)

                elif choice == len(phones) + 4 and len(phones) > 1:
                    print_header("Multiple Accounts Check")
                    for phone in sessions:
                        if phone not in clients:
                            session = sessions[phone]
                            proxy = session.get("proxy")
                            client_kwargs = {
                                "session": StringSession(session["session"]),
                                "api_id": int(session["api_id"]),
                                "api_hash": session["api_hash"]
                            }
                            if proxy and ':' in proxy:
                                proxy_parts = proxy.split(':')
                                if len(proxy_parts) == 2:
                                    client_kwargs["proxy"] = ("socks5", proxy_parts[0], int(proxy_parts[1]))
                                elif len(proxy_parts) == 3:
                                    client_kwargs["proxy"] = ("socks5", proxy_parts[0], int(proxy_parts[1]), proxy_parts[2])
                            client = TelegramClient(**client_kwargs)
                            clients[phone] = client
                            await client.connect()
                            if not await authenticate_client(client, phone, proxy_manager):
                                await client.disconnect()
                                del clients[phone]
                                continue
                    if clients:
                        await process_multiple_accounts(clients, account_manager, proxy_manager, skip_flooded=args.skip_flooded)
                    else:
                        print_colored("No available accounts!", Fore.RED)

elif choice == len(phones) + 5:
    sessions = await account_manager.load_sessions()
    if not sessions:
        print_colored("No sessions found! Create one first.", Fore.RED)
        continue
    for phone in sessions:
        if phone not in clients:
            session = sessions[phone]
            proxy = session.get("proxy")
            client_kwargs = {
                "session": StringSession(session["session"]),
                "api_id": int(session["api_id"]),
                "api_hash": session["api_hash"]
            }
            if proxy and ':' in proxy:
                proxy_parts = proxy.split(':')
                if len(proxy_parts) == 2:
                    client_kwargs["proxy"] = ("socks5", proxy_parts[0], int(proxy_parts[1]))
                elif len(proxy_parts) == 3:
                    client_kwargs["proxy"] = ("socks5", proxy_parts[0], int(proxy_parts[1]), proxy_parts[2])
            client = TelegramClient(**client_kwargs)
            clients[phone] = client
            await client.connect()
            if not await authenticate_client(client, phone, proxy_manager):
                await client.disconnect()
                del clients[phone]
                print_colored(f"Failed to authenticate {mask_phone(phone)}", Fore.RED)
                continue
    if not clients:
        print_colored("No active sessions available!", Fore.RED)
        continue
    await hunting_mode(clients, account_manager, proxy_manager)

                else:
                    print_colored("Invalid choice!", Fore.RED)

            except ValueError:
                print_colored("Invalid choice!", Fore.RED)

            print_header("Continue?")
            print_colored("[1] Back to main menu", Fore.WHITE)
            print_colored("[Any other key] Exit", Fore.WHITE)
            print_footer()
            choice = input(f"{Fore.BLUE}Enter choice: {Style.RESET_ALL} ")
            if choice != "1":
                break

    except KeyboardInterrupt:
        print_colored("Script stopped!", Fore.GREEN)
        logger.info("Script stopped by user")
    except Exception as e:
        print_colored(f"Unexpected error: {str(e)}", Fore.RED)
        logger.error(f"Unexpected error: {str(e)}")
    finally:
        for client in clients.values():
            try:
                await client.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting client: {str(e)}")
        print_colored("Cleanup completed. Thanks for using!", Fore.GREEN)

if __name__ == "__main__"
    asyncio.run(main())
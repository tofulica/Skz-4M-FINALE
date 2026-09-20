import os
import json
import csv
import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID_RAW = os.getenv("GUILD_ID")
try:
    GUILD_ID = int(GUILD_ID_RAW) if GUILD_ID_RAW else None
except ValueError:
    GUILD_ID = None

SHEET_CSV_URL = os.getenv("SHEET_CSV_URL")

FARM_REPORT_CHANNEL_NAME = (
    os.getenv("FARM_REPORT_CHANNEL_NAME") or "farms"
).strip().lower().replace("#", "")

CONTENT_REQUEST_CHANNEL_NAME = (
    os.getenv("CONTENT_REQUEST_CHANNEL_NAME") or "content-requests"
).strip().lower().replace("#", "")

APPROVAL_CHANNEL_NAME = (
    os.getenv("APPROVAL_CHANNEL_NAME") or "approvals"
).strip().lower().replace("#", "")

TZ = ZoneInfo("Europe/Berlin")

DATA_DIR = (
    os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    or os.getenv("DATA_DIR")
    or "."
)

os.makedirs(DATA_DIR, exist_ok=True)

DATA_FILE = os.path.join(
    DATA_DIR,
    "clock_data.json"
)

EARLY_STREAK_WINDOW_MINUTES = 180
LATE_STREAK_WINDOW_MINUTES = 180

NO_CLOCKIN_ALERT_AFTER_MINUTES = 5
NO_CLOCKIN_ALERT_WINDOW_MINUTES = 10

ALLSTATUS_NEXT_SHIFT_SWITCH_MINUTES = 15

STREAK_SHIFT_TIMES_RAW = (
    os.getenv("STREAK_SHIFT_TIMES")
    or "02:00,10:00,18:00"
)

STREAK_SHIFT_TIMES = [
    shift_time.strip()
    for shift_time in STREAK_SHIFT_TIMES_RAW.split(",")
    if shift_time.strip()
]


def load_clock_data():
    if not os.path.exists(DATA_FILE):
        return {
            "reminded": {},
            "no_clockin_alerts": {},
            "clocked_in_channels": {},
            "announcement_batches": [],
            "streaks": {},
            "mma_requests": {}
        }

    with open(DATA_FILE, "r") as f:
        data = json.load(f)

    if "reminded" not in data:
        data["reminded"] = {}

    if "no_clockin_alerts" not in data:
        data["no_clockin_alerts"] = {}

    if "clocked_in_channels" not in data:
        data["clocked_in_channels"] = {}

    if "announcement_batches" not in data:
        data["announcement_batches"] = []

    if "streaks" not in data:
        data["streaks"] = {}

    if "mma_requests" not in data:
        data["mma_requests"] = {}

    for channel_name, value in list(
        data["clocked_in_channels"].items()
    ):
        if isinstance(value, dict):
            data["clocked_in_channels"][channel_name] = [
                value
            ]

    return data


def save_clock_data():
    with open(DATA_FILE, "w") as f:
        json.dump(
            clock_data,
            f,
            indent=4
        )


def load_schedule_from_csv():
    response = requests.get(
        SHEET_CSV_URL
    )

    response.raise_for_status()

    csv_text = response.text

    reader = csv.DictReader(
        io.StringIO(csv_text)
    )

    schedule = []

    for row in reader:
        if not row.get("account"):
            continue

        announcement_channel = ""

        if (
            "announcement_channel_name" in row
            and row["announcement_channel_name"]
        ):
            announcement_channel = (
                row["announcement_channel_name"]
                .strip()
                .lower()
            )

        schedule.append({
            "account": row[
                "account"
            ].strip(),

            "channel_name": row[
                "channel_name"
            ].strip().lower(),

            "shift_time": row[
                "shift_time"
            ].strip(),

            "scheduled_chatter_username": (
                row[
                    "scheduled_chatter_username"
                ]
                .strip()
                .lower()
                .replace("@", "")
            ),

            "supervisor_role_name": (
                row[
                    "supervisor_role_name"
                ]
                .strip()
                .replace("@", "")
            ),

            "announcement_channel_name": (
                announcement_channel
            )
        })

    return schedule


clock_data = load_clock_data()

schedule_cache = []

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    print(
        f"SAW MESSAGE: "
        f"{message.author} said "
        f"{message.content}"
    )

    await bot.process_commands(
        message
    )


def get_main_guild(ctx=None):
    if ctx and ctx.guild:
        return ctx.guild

    if GUILD_ID:
        guild = bot.get_guild(
            GUILD_ID
        )

        if guild is not None:
            return guild

    if len(bot.guilds) == 1:
        return bot.guilds[0]

    return None


def normalize_name(value):
    if not value:
        return ""

    return (
        value
        .lower()
        .replace("@", "")
        .replace("#", "")
        .strip()
    )


def get_channel_prefix(channel_name):
    channel_name = normalize_name(
        channel_name
    )

    if "-" in channel_name:
        return (
            channel_name
            .split("-")[0]
            .strip()
        )

    return channel_name.strip()


def get_channel_key_from_parts(
    guild_id,
    channel_name
):
    guild_part = (
        str(guild_id)
        if guild_id
        else "unknown-guild"
    )

    return (
        f"{guild_part}:"
        f"{normalize_name(channel_name)}"
    )


def get_channel_key(ctx):
    guild_id = (
        ctx.guild.id
        if ctx.guild
        else None
    )

    return get_channel_key_from_parts(
        guild_id,
        ctx.channel.name
    )


def get_channel_name_from_key(
    channel_key
):
    if ":" in channel_key:
        return channel_key.split(
            ":",
            1
        )[1]

    return channel_key


def get_channel_storage_key_for_checkout(
    guild_id,
    channel_name
):
    channel_name = normalize_name(
        channel_name
    )

    channel_key = (
        get_channel_key_from_parts(
            guild_id,
            channel_name
        )
    )

    if (
        channel_key
        in clock_data[
            "clocked_in_channels"
        ]
    ):
        return channel_key

    if (
        channel_name
        in clock_data[
            "clocked_in_channels"
        ]
    ):
        return channel_name

    return channel_key


def get_clockins_for_guild_channel(
    guild_id,
    channel_name
):
    channel_name = normalize_name(
        channel_name
    )

    channel_key = (
        get_channel_key_from_parts(
            guild_id,
            channel_name
        )
    )

    if (
        channel_key
        in clock_data[
            "clocked_in_channels"
        ]
    ):
        return clock_data[
            "clocked_in_channels"
        ].get(
            channel_key,
            []
        )

    return clock_data[
        "clocked_in_channels"
    ].get(
        channel_name,
        []
    )


def is_channel_clocked_in(
    guild_id,
    channel_name
):
    clockins = (
        get_clockins_for_guild_channel(
            guild_id,
            channel_name
        )
    )

    return len(clockins) > 0


def parse_clockin_time(clockin):
    try:
        return datetime.strptime(
            clockin.get(
                "time",
                ""
            ),
            "%Y-%m-%d %H:%M:%S"
        ).replace(
            tzinfo=TZ
        )

    except Exception:
        return None


def sendable_chunks(
    text,
    limit=1900
):
    chunks = []
    current = ""

    for line in text.splitlines(True):
        if (
            len(current)
            + len(line)
            > limit
        ):
            if current:
                chunks.append(
                    current
                )

            current = line

        else:
            current += line

    if current:
        chunks.append(
            current
        )

    return chunks


def member_matches_username(
    member,
    username
):
    username = normalize_name(
        username
    )

    possible_names = [
        normalize_name(
            member.name
        ),
        normalize_name(
            member.display_name
        )
    ]

    if member.global_name:
        possible_names.append(
            normalize_name(
                member.global_name
            )
        )

    return username in possible_names


def find_member_by_username(
    guild,
    username
):
    for member in guild.members:
        if member_matches_username(
            member,
            username
        ):
            return member

    return None


def find_member_by_identifier(
    guild,
    identifier
):
    if not identifier:
        return None

    identifier = identifier.strip()

    clean_identifier = (
        identifier
        .replace("<@", "")
        .replace(">", "")
        .replace("!", "")
    )

    if clean_identifier.isdigit():
        member = guild.get_member(
            int(clean_identifier)
        )

        if member is not None:
            return member

    return find_member_by_username(
        guild,
        identifier
    )


def find_channel_by_name(
    guild,
    channel_name
):
    channel_name = normalize_name(
        channel_name
    )

    for channel in guild.text_channels:
        if (
            normalize_name(
                channel.name
            )
            == channel_name
        ):
            return channel

    return None


def find_role_by_name(
    guild,
    role_name
):
    role_name = normalize_name(
        role_name
    )

    for role in guild.roles:
        if (
            normalize_name(
                role.name
            )
            == role_name
        ):
            return role

    return None


def get_unique_announcement_channels():
    channels = []

    for shift in schedule_cache:
        channel_name = (
            shift
            .get(
                "announcement_channel_name",
                ""
            )
            .strip()
            .lower()
        )

        if (
            channel_name
            and channel_name
            not in channels
        ):
            channels.append(
                channel_name
            )

    return channels


def get_unique_rules_channels(
    guild
):
    channels = []

    for channel in guild.text_channels:
        channel_name = normalize_name(
            channel.name
        )

        if (
            "rules" in channel_name
            and channel_name
            not in channels
        ):
            channels.append(
                channel_name
            )

    return channels


def get_unique_training_channels(
    guild
):
    channels = []

    for channel in guild.text_channels:
        channel_name = normalize_name(
            channel.name
        )

        if (
            "training"
            in channel_name
            and channel_name
            not in channels
        ):
            channels.append(
                channel_name
            )

    return channels


def find_account_by_source_channel(
    channel_name
):
    channel_name = normalize_name(
        channel_name
    )

    source_prefix = get_channel_prefix(
        channel_name
    )

    for shift in schedule_cache:
        possible_channels = [
            normalize_name(
                shift.get(
                    "channel_name"
                )
            ),
            normalize_name(
                shift.get(
                    "announcement_channel_name"
                )
            )
        ]

        if (
            channel_name
            in possible_channels
        ):
            return shift.get(
                "account",
                "Unknown Model"
            )

    for shift in schedule_cache:
        possible_channels = [
            normalize_name(
                shift.get(
                    "channel_name"
                )
            ),
            normalize_name(
                shift.get(
                    "announcement_channel_name"
                )
            )
        ]

        for possible_channel in possible_channels:
            if (
                possible_channel
                and get_channel_prefix(
                    possible_channel
                )
                == source_prefix
            ):
                return shift.get(
                    "account",
                    "Unknown Model"
                )

    return "Unknown Model"


def get_or_create_streak_data(
    member
):
    user_id = str(
        member.id
    )

    if "streaks" not in clock_data:
        clock_data["streaks"] = {}

    if (
        user_id
        not in clock_data["streaks"]
    ):
        clock_data[
            "streaks"
        ][user_id] = {}

    streak_data = clock_data[
        "streaks"
    ][user_id]

    defaults = {
        "username": member.name,
        "display_name": member.display_name,
        "streak": 0,
        "last_counted_date": None,
        "last_reset_date": None,
        "first_clockin_date": None,
        "first_clockin_status": None,
        "first_clockin_channel": None,
        "first_clockin_time": None,
        "first_clockin_shift_time": None,
        "last_clockin_date": None,
        "last_clockin_channel": None,
        "last_clockin_guild_id": None,
        "manual_set_date": None,
        "manual_set_by": None,
        "manual_set_channel": None
    }

    for key, value in defaults.items():
        if key not in streak_data:
            streak_data[key] = value

    streak_data[
        "username"
    ] = member.name

    streak_data[
        "display_name"
    ] = member.display_name

    return streak_data


def get_current_streak(member):
    streak_data = (
        get_or_create_streak_data(
            member
        )
    )

    return streak_data.get(
        "streak",
        0
    )


def can_manage_streaks(member):
    if (
        member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
    ):
        return True

    allowed_roles = [
        "owner",
        "admin",
        "admins",
        "manager",
        "managers",
        "supervisor",
        "supervisors",
        "gm",
        "management",
        "menadzer",
        "menadzeri"
    ]

    for role in member.roles:
        if (
            normalize_name(
                role.name
            )
            in allowed_roles
        ):
            return True

    return False


def can_manage_broadcasts(member):
    return can_manage_streaks(
        member
    )


def get_streak_shift_datetimes():
    now = datetime.now(TZ)

    shifts = []

    for shift_time in STREAK_SHIFT_TIMES:
        for day_offset in [-1, 0, 1]:
            shift_date = (
                now
                + timedelta(
                    days=day_offset
                )
            ).strftime(
                "%Y-%m-%d"
            )

            try:
                shift_datetime = (
                    datetime.strptime(
                        f"{shift_date} "
                        f"{shift_time}",
                        "%Y-%m-%d %H:%M"
                    ).replace(
                        tzinfo=TZ
                    )
                )

            except ValueError:
                continue

            shifts.append(
                shift_datetime
            )

    shifts = sorted(
        list(
            set(shifts)
        )
    )

    return shifts


def find_independent_streak_clockin_status():
    now = datetime.now(
        TZ
    ).replace(
        second=0,
        microsecond=0
    )

    possible_matches = []

    for shift_datetime in (
        get_streak_shift_datetimes()
    ):
        early_start = (
            shift_datetime
            - timedelta(
                minutes=(
                    EARLY_STREAK_WINDOW_MINUTES
                )
            )
        )

        late_end = (
            shift_datetime
            + timedelta(
                minutes=(
                    LATE_STREAK_WINDOW_MINUTES
                )
            )
        )

        if (
            early_start
            <= now
            <= shift_datetime
        ):
            distance = abs(
                (
                    shift_datetime
                    - now
                ).total_seconds()
            )

            possible_matches.append({
                "status": "on_time",
                "shift_datetime": (
                    shift_datetime
                ),
                "distance": distance
            })

        elif (
            shift_datetime
            < now
            <= late_end
        ):
            distance = abs(
                (
                    now
                    - shift_datetime
                ).total_seconds()
            )

            possible_matches.append({
                "status": "late",
                "shift_datetime": (
                    shift_datetime
                ),
                "distance": distance
            })

    if not possible_matches:
        return (
            "outside_window",
            None
        )

    possible_matches.sort(
        key=lambda item: item[
            "distance"
        ]
    )

    best_match = (
        possible_matches[0]
    )

    return (
        best_match["status"],
        best_match
    )


def update_streak_for_clockin(
    member,
    channel_name,
    guild_id=None
):
    today = datetime.now(
        TZ
    ).strftime(
        "%Y-%m-%d"
    )

    now_text = datetime.now(
        TZ
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    streak_data = (
        get_or_create_streak_data(
            member
        )
    )

    streak_data[
        "last_clockin_date"
    ] = today

    streak_data[
        "last_clockin_channel"
    ] = normalize_name(
        channel_name
    )

    streak_data[
        "last_clockin_guild_id"
    ] = (
        str(guild_id)
        if guild_id
        else None
    )

    if (
        streak_data.get(
            "first_clockin_date"
        )
        == today
    ):
        return (
            streak_data.get(
                "streak",
                0
            ),
            "already_counted_today"
        )

    streak_status, shift_info = (
        find_independent_streak_clockin_status()
    )

    streak_data[
        "first_clockin_date"
    ] = today

    streak_data[
        "first_clockin_status"
    ] = streak_status

    streak_data[
        "first_clockin_channel"
    ] = normalize_name(
        channel_name
    )

    streak_data[
        "first_clockin_time"
    ] = now_text

    if shift_info is not None:
        streak_data[
            "first_clockin_shift_time"
        ] = (
            shift_info[
                "shift_datetime"
            ].strftime(
                "%H:%M"
            )
        )

    else:
        streak_data[
            "first_clockin_shift_time"
        ] = None

    if streak_status == "on_time":
        if (
            streak_data.get(
                "last_counted_date"
            )
            != today
        ):
            streak_data[
                "streak"
            ] = (
                streak_data.get(
                    "streak",
                    0
                )
                + 1
            )

            streak_data[
                "last_counted_date"
            ] = today

        return (
            streak_data.get(
                "streak",
                0
            ),
            "counted"
        )

    if streak_status == "late":
        streak_data[
            "streak"
        ] = 0

        streak_data[
            "last_reset_date"
        ] = today

        streak_data[
            "last_counted_date"
        ] = today

        return (
            streak_data.get(
                "streak",
                0
            ),
            "reset_late"
        )

    return (
        streak_data.get(
            "streak",
            0
        ),
        "outside_window"
    )


def get_shift_datetimes_for_channel(
    channel_name
):
    channel_name = normalize_name(
        channel_name
    )

    now = datetime.now(TZ)

    shifts = []

    for shift in schedule_cache:
        if (
            normalize_name(
                shift.get(
                    "channel_name"
                )
            )
            != channel_name
        ):
            continue

        for day_offset in [-1, 0, 1]:
            shift_date = (
                now
                + timedelta(
                    days=day_offset
                )
            ).strftime(
                "%Y-%m-%d"
            )

            try:
                shift_datetime = (
                    datetime.strptime(
                        f"{shift_date} "
                        f"{shift['shift_time']}",
                        "%Y-%m-%d %H:%M"
                    ).replace(
                        tzinfo=TZ
                    )
                )

            except ValueError:
                continue

            shifts.append(
                shift_datetime
            )

    shifts = sorted(
        list(
            set(shifts)
        )
    )

    return shifts


def get_allstatus_target_shift(
    channel_name
):
    shifts = (
        get_shift_datetimes_for_channel(
            channel_name
        )
    )

    if not shifts:
        return None

    now = datetime.now(TZ)

    target_shift = None

    for shift_datetime in shifts:
        switch_time = (
            shift_datetime
            - timedelta(
                minutes=(
                    ALLSTATUS_NEXT_SHIFT_SWITCH_MINUTES
                )
            )
        )

        if now >= switch_time:
            target_shift = (
                shift_datetime
            )

    if target_shift is not None:
        return target_shift

    for shift_datetime in shifts:
        if shift_datetime > now:
            return shift_datetime

    return shifts[-1]


def get_allstatus_valid_clockins(
    guild_id,
    channel_name
):
    clockins = (
        get_clockins_for_guild_channel(
            guild_id,
            channel_name
        )
    )

    target_shift = (
        get_allstatus_target_shift(
            channel_name
        )
    )

    if target_shift is None:
        return (
            clockins,
            None
        )

    cutoff_time = (
        target_shift
        - timedelta(
            minutes=(
                EARLY_STREAK_WINDOW_MINUTES
            )
        )
    )

    valid_clockins = []

    for clockin in clockins:
        clockin_time = (
            parse_clockin_time(
                clockin
            )
        )

        if clockin_time is None:
            continue

        if clockin_time >= cutoff_time:
            valid_clockins.append(
                clockin
            )

    return (
        valid_clockins,
        target_shift
    )


def create_announcement_batch_id():
    return datetime.now(
        TZ
    ).strftime(
        "%Y%m%d-%H%M%S"
    )


def get_latest_active_announcement_batch():
    batches = clock_data.get(
        "announcement_batches",
        []
    )

    for batch in reversed(batches):
        if not batch.get(
            "deleted",
            False
        ):
            return batch

    return None


def get_latest_active_batch_by_type(
    batch_type
):
    batches = clock_data.get(
        "announcement_batches",
        []
    )

    for batch in reversed(batches):
        if (
            not batch.get(
                "deleted",
                False
            )
            and batch.get(
                "type"
            )
            == batch_type
        ):
            return batch

    return None


async def delete_saved_batch_messages(
    ctx,
    batch,
    label="announcement"
):
    guild = get_main_guild(ctx)

    if guild is None:
        await ctx.send(
            "❌ Guild not found. "
            "Check GUILD_ID."
        )
        return

    deleted_count = 0
    already_deleted_count = 0
    failed_messages = []

    for message_info in batch.get(
        "messages",
        []
    ):
        channel_id = (
            message_info.get(
                "channel_id"
            )
        )

        message_id = (
            message_info.get(
                "message_id"
            )
        )

        channel_name = (
            message_info.get(
                "channel_name",
                "unknown-channel"
            )
        )

        channel = (
            bot.get_channel(
                int(channel_id)
            )
            if channel_id
            else None
        )

        if channel is None:
            channel = find_channel_by_name(
                guild,
                channel_name
            )

        if channel is None:
            failed_messages.append(
                f"#{channel_name} "
                f"— channel not found"
            )
            continue

        try:
            discord_message = (
                await channel.fetch_message(
                    int(message_id)
                )
            )

            await discord_message.delete()

            deleted_count += 1

        except discord.NotFound:
            already_deleted_count += 1

        except discord.Forbidden:
            failed_messages.append(
                f"#{channel_name} "
                f"— no permission to delete"
            )

        except Exception as e:
            print(
                f"Failed to delete "
                f"{label} from "
                f"{channel_name}: {e}"
            )

            failed_messages.append(
                f"#{channel_name} "
                f"— delete failed"
            )

    batch["deleted"] = True

    batch[
        "deleted_at"
    ] = datetime.now(
        TZ
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    batch[
        "deleted_by"
    ] = ctx.author.name

    save_clock_data()

    reply = ""

    reply += (
        f"🗑️ Delete finished for "
        f"{label} batch "
        f"`{batch.get('batch_id')}`."
        f"\n\n"
    )

    reply += (
        f"Deleted: "
        f"**{deleted_count}** messages\n"
    )

    reply += (
        f"Already deleted / not found: "
        f"**{already_deleted_count}** messages\n"
    )

    if failed_messages:
        reply += "\n❌ Failed:\n"
        reply += "\n".join(
            failed_messages
        )

    await ctx.send(reply)


# =========================
# MMA APPROVAL SYSTEM
# =========================

def create_mma_request_id():
    return datetime.now(
        TZ
    ).strftime(
        "%Y%m%d-%H%M%S-%f"
    )


def get_mma_request(request_id):
    return clock_data.get(
        "mma_requests",
        {}
    ).get(
        request_id
    )


def build_mma_embed(
    request_data
):
    status = request_data.get(
        "status",
        "pending"
    )

    if status == "approved":
        color = discord.Color.green()
        status_text = "✅ APPROVED"

    elif status == "declined":
        color = discord.Color.red()
        status_text = "❌ DECLINED"

    else:
        color = discord.Color.orange()
        status_text = "⏳ PENDING APPROVAL"

    mass_message = request_data.get(
        "mass_message",
        ""
    )

    embed = discord.Embed(
        title="📨 Mass Message Approval",
        description=mass_message,
        color=color,
        timestamp=datetime.now(TZ)
    )

    model_name = request_data.get(
        "model_name"
    )

    if model_name:
        embed.add_field(
            name="Model",
            value=model_name,
            inline=True
        )

    embed.add_field(
        name="Submitted by",
        value=(
            f"<@{request_data.get('requester_id')}>"
        ),
        inline=True
    )

    embed.add_field(
        name="Source",
        value=(
            f"<#{request_data.get('source_channel_id')}>"
        ),
        inline=True
    )

    embed.add_field(
        name="Status",
        value=status_text,
        inline=True
    )

    attachments = request_data.get(
        "attachments",
        []
    )

    if attachments:
        attachment_lines = []

        for index, attachment_url in enumerate(
            attachments,
            start=1
        ):
            attachment_lines.append(
                f"[Attachment {index}]"
                f"({attachment_url})"
            )

        attachment_text = "\n".join(
            attachment_lines
        )

        if len(attachment_text) > 1024:
            attachment_text = (
                attachment_text[:1000]
                + "..."
            )

        embed.add_field(
            name="Attachments",
            value=attachment_text,
            inline=False
        )

    if status in [
        "approved",
        "declined"
    ]:
        decided_by = request_data.get(
            "decided_by"
        )

        decided_at = request_data.get(
            "decided_at"
        )

        if decided_by:
            embed.add_field(
                name="Reviewed by",
                value=(
                    f"<@{decided_by}>"
                ),
                inline=True
            )

        if decided_at:
            embed.add_field(
                name="Reviewed at",
                value=decided_at,
                inline=True
            )

    embed.set_footer(
        text=(
            f"Approval ID: "
            f"{request_data.get('request_id', 'unknown')}"
        )
    )

    return embed


async def get_channel_from_id(
    channel_id
):
    if not channel_id:
        return None

    try:
        channel_id = int(
            channel_id
        )

    except (
        TypeError,
        ValueError
    ):
        return None

    channel = bot.get_channel(
        channel_id
    )

    if channel is not None:
        return channel

    try:
        return await bot.fetch_channel(
            channel_id
        )

    except Exception as e:
        print(
            f"Could not fetch channel "
            f"{channel_id}: {e}"
        )

        return None


async def handle_mma_decision(
    interaction,
    request_id,
    decision
):
    request_data = get_mma_request(
        request_id
    )

    if request_data is None:
        await interaction.response.send_message(
            "❌ This approval request "
            "no longer exists.",
            ephemeral=True
        )
        return

    current_status = request_data.get(
        "status",
        "pending"
    )

    if current_status != "pending":
        await interaction.response.send_message(
            f"⚠️ This mass message has already "
            f"been **{current_status}**.",
            ephemeral=True
        )
        return

    if decision == "approved":
        request_data[
            "status"
        ] = "approved"

    else:
        request_data[
            "status"
        ] = "declined"

    request_data[
        "decided_by"
    ] = str(
        interaction.user.id
    )

    request_data[
        "decided_by_name"
    ] = interaction.user.display_name

    request_data[
        "decided_at"
    ] = datetime.now(
        TZ
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    save_clock_data()

    disabled_view = MMAApprovalView(
        request_id=request_id,
        disabled=True
    )

    embed = build_mma_embed(
        request_data
    )

    await interaction.response.edit_message(
        embed=embed,
        view=disabled_view
    )

    source_channel = (
        await get_channel_from_id(
            request_data.get(
                "source_channel_id"
            )
        )
    )

    if source_channel is None:
        print(
            f"MMA RESULT ERROR | "
            f"Could not find source channel "
            f"{request_data.get('source_channel_id')} "
            f"for request {request_id}"
        )
        return

    requester_id = request_data.get(
        "requester_id"
    )

    if decision == "approved":
        result_text = (
            f"✅ <@{requester_id}> "
            f"your Mass Message has been approved "
            f"and is ready to send!\n\n"
            f"Approved by: "
            f"{interaction.user.mention}"
        )

    else:
        result_text = (
            f"❌ <@{requester_id}> "
            f"your Mass Message was declined "
            f"and needs changes before sending.\n\n"
            f"Reviewed by: "
            f"{interaction.user.mention}"
        )

    await source_channel.send(
        result_text
    )


class MMAApprovalView(
    discord.ui.View
):
    def __init__(
        self,
        request_id,
        disabled=False
    ):
        super().__init__(
            timeout=None
        )

        self.request_id = request_id

        approve_button = (
            discord.ui.Button(
                label="Approve",
                emoji="✅",
                style=(
                    discord.ButtonStyle.success
                ),
                custom_id=(
                    f"mma_approve_"
                    f"{request_id}"
                ),
                disabled=disabled
            )
        )

        decline_button = (
            discord.ui.Button(
                label="Decline",
                emoji="❌",
                style=(
                    discord.ButtonStyle.danger
                ),
                custom_id=(
                    f"mma_decline_"
                    f"{request_id}"
                ),
                disabled=disabled
            )
        )

        approve_button.callback = (
            self.approve_callback
        )

        decline_button.callback = (
            self.decline_callback
        )

        self.add_item(
            approve_button
        )

        self.add_item(
            decline_button
        )

    async def approve_callback(
        self,
        interaction
    ):
        await handle_mma_decision(
            interaction,
            self.request_id,
            "approved"
        )

    async def decline_callback(
        self,
        interaction
    ):
        await handle_mma_decision(
            interaction,
            self.request_id,
            "declined"
        )


mma_views_registered = False


@bot.event
async def on_ready():
    global schedule_cache
    global mma_views_registered

    schedule_cache = (
        load_schedule_from_csv()
    )

    if not shift_reminder_loop.is_running():
        shift_reminder_loop.start()

    if not mma_views_registered:
        pending_count = 0

        for (
            request_id,
            request_data
        ) in clock_data.get(
            "mma_requests",
            {}
        ).items():

            if (
                request_data.get(
                    "status"
                )
                == "pending"
            ):
                bot.add_view(
                    MMAApprovalView(
                        request_id
                    )
                )

                pending_count += 1

        mma_views_registered = True

        print(
            f"Registered "
            f"{pending_count} pending "
            f"MMA approval views."
        )

    print(
        f"Logged in as {bot.user}"
    )

    print(
        f"Loaded "
        f"{len(schedule_cache)} shifts."
    )

    print(
        f"Using data file: "
        f"{DATA_FILE}"
    )

    print(
        f"Streak shift times: "
        f"{', '.join(STREAK_SHIFT_TIMES)}"
    )

    print(
        "Servers bot can see:"
    )

    for guild in bot.guilds:
        print(
            f"- {guild.name} | "
            f"{guild.id}"
        )


@bot.command(name="ci")
async def ci(ctx):
    current_channel_name = (
        normalize_name(
            ctx.channel.name
        )
    )

    if (
        "clock"
        not in current_channel_name
    ):
        await ctx.send(
            "❌ Clock-ins must be submitted "
            "in the model's **clock-in** channel."
        )
        return

    user_id = str(
        ctx.author.id
    )

    username = (
        ctx.author.name.lower()
    )

    display_name = (
        ctx.author.display_name
    )

    channel_name = normalize_name(
        ctx.channel.name
    )

    channel_key = (
        get_channel_key(ctx)
    )

    guild_id = (
        ctx.guild.id
        if ctx.guild
        else None
    )

    now = datetime.now(
        TZ
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    if (
        channel_key
        not in clock_data[
            "clocked_in_channels"
        ]
    ):
        clock_data[
            "clocked_in_channels"
        ][channel_key] = []

    for clockin in clock_data[
        "clocked_in_channels"
    ][channel_key]:

        old_user_id = clockin.get(
            "user_id"
        )

        old_username = clockin.get(
            "username"
        )

        if (
            old_user_id == user_id
            or old_username == username
        ):
            await ctx.message.add_reaction(
                "❌"
            )
            return

    clock_data[
        "clocked_in_channels"
    ][channel_key].append({
        "user_id": user_id,
        "username": username,
        "display_name": display_name,
        "time": now,
        "channel_name": channel_name,
        "guild_id": (
            str(guild_id)
            if guild_id
            else None
        )
    })

    streak, streak_status = (
        update_streak_for_clockin(
            ctx.author,
            channel_name,
            guild_id
        )
    )

    save_clock_data()

    await ctx.message.add_reaction(
        "✅"
    )

    if streak_status == "reset_late":
        await ctx.send(
            f"🔥 Streak {streak} - "
            f"your streak has been reset "
            f"because of a late clock-in"
        )

    else:
        await ctx.send(
            f"🔥 Streak {streak}"
        )


@bot.command(name="co")
async def co(ctx):
    current_channel_name = (
        normalize_name(
            ctx.channel.name
        )
    )

    if (
        "clock"
        not in current_channel_name
    ):
        await ctx.send(
            "❌ Clock-outs must be submitted "
            "in the model's **clock-in** channel."
        )
        return

    user_id = str(
        ctx.author.id
    )

    username = (
        ctx.author.name.lower()
    )

    channel_name = normalize_name(
        ctx.channel.name
    )

    guild_id = (
        ctx.guild.id
        if ctx.guild
        else None
    )

    channel_key = (
        get_channel_storage_key_for_checkout(
            guild_id,
            channel_name
        )
    )

    now = datetime.now(TZ)

    if (
        channel_key
        not in clock_data[
            "clocked_in_channels"
        ]
    ):
        await ctx.message.add_reaction(
            "❌"
        )
        return

    user_clockin = None

    for clockin in clock_data[
        "clocked_in_channels"
    ][channel_key]:

        old_user_id = clockin.get(
            "user_id"
        )

        old_username = clockin.get(
            "username"
        )

        if (
            old_user_id == user_id
            or old_username == username
        ):
            user_clockin = clockin
            break

    if user_clockin is None:
        await ctx.message.add_reaction(
            "❌"
        )
        return

    start = datetime.strptime(
        user_clockin["time"],
        "%Y-%m-%d %H:%M:%S"
    ).replace(
        tzinfo=TZ
    )

    duration = now - start

    clock_data[
        "clocked_in_channels"
    ][channel_key].remove(
        user_clockin
    )

    if not clock_data[
        "clocked_in_channels"
    ][channel_key]:

        del clock_data[
            "clocked_in_channels"
        ][channel_key]

    save_clock_data()

    total_minutes = int(
        duration.total_seconds()
        // 60
    )

    hours = (
        total_minutes
        // 60
    )

    minutes = (
        total_minutes
        % 60
    )

    print(
        f"{username} clocked out "
        f"from {channel_name}. "
        f"Shift duration: "
        f"{hours}h {minutes}m"
    )

    await ctx.message.add_reaction(
        "✅"
    )


@bot.command()
async def status(ctx):
    if not clock_data[
        "clocked_in_channels"
    ]:
        await ctx.send(
            "Nobody is currently clocked "
            "in on any account."
        )
        return

    current_guild_id = (
        str(ctx.guild.id)
        if ctx.guild
        else None
    )

    msg = (
        "**Currently clocked in "
        "by account/channel:**\n"
    )

    shown_any = False

    for (
        channel_key,
        clockins
    ) in clock_data[
        "clocked_in_channels"
    ].items():

        if (
            current_guild_id
            and ":" in channel_key
            and not channel_key.startswith(
                f"{current_guild_id}:"
            )
        ):
            continue

        display_channel_name = (
            get_channel_name_from_key(
                channel_key
            )
        )

        if clockins:
            display_channel_name = (
                clockins[0].get(
                    "channel_name",
                    display_channel_name
                )
            )

        msg += (
            f"\n**{display_channel_name}**\n"
        )

        for clockin in clockins:
            name = (
                clockin.get(
                    "display_name"
                )
                or clockin.get(
                    "username",
                    "unknown"
                )
            )

            time = clockin.get(
                "time",
                "unknown time"
            )

            msg += (
                f"- {name} since "
                f"**{time}**\n"
            )

            shown_any = True

    if not shown_any:
        await ctx.send(
            "Nobody is currently clocked "
            "in on this server."
        )
        return

    for chunk in sendable_chunks(
        msg
    ):
        await ctx.send(
            chunk
        )


@bot.command(name="allstatus")
async def allstatus(ctx):
    guild = get_main_guild(ctx)

    if guild is None:
        await ctx.send(
            "❌ Guild not found. "
            "Check GUILD_ID."
        )
        return

    if not schedule_cache:
        await ctx.send(
            "❌ Schedule is empty. "
            "Use `!reloadschedule` first."
        )
        return

    guild_id = guild.id

    unique_channels = []
    seen_channels = set()

    for shift in schedule_cache:
        channel_name = normalize_name(
            shift.get(
                "channel_name"
            )
        )

        if (
            not channel_name
            or channel_name
            in seen_channels
        ):
            continue

        seen_channels.add(
            channel_name
        )

        unique_channels.append({
            "account": shift.get(
                "account",
                "Unknown Model"
            ),
            "channel_name": (
                channel_name
            )
        })

    now_text = datetime.now(
        TZ
    ).strftime(
        "%H:%M"
    )

    msg = (
        f"**📊 All Model Status** "
        f"— `{now_text}`\n"
    )

    msg += (
        "_15 minutes before the next shift, "
        "old clock-ins from the previous shift "
        "do not count here._\n\n"
    )

    for item in unique_channels:
        account = item[
            "account"
        ]

        channel_name = item[
            "channel_name"
        ]

        (
            valid_clockins,
            target_shift
        ) = (
            get_allstatus_valid_clockins(
                guild_id,
                channel_name
            )
        )

        if target_shift is not None:
            shift_text = (
                target_shift.strftime(
                    "%H:%M"
                )
            )

        else:
            shift_text = "unknown"

        if valid_clockins:
            names = []

            for clockin in valid_clockins:
                name = (
                    clockin.get(
                        "display_name"
                    )
                    or clockin.get(
                        "username",
                        "unknown"
                    )
                )

                clockin_time = (
                    clockin.get(
                        "time",
                        "unknown time"
                    )
                )

                names.append(
                    f"{name} since "
                    f"`{clockin_time}`"
                )

            msg += (
                f"✅ **{account}** "
                f"(`#{channel_name}` / "
                f"shift `{shift_text}`)\n"
            )

            msg += (
                "   "
                + "; ".join(names)
                + "\n"
            )

        else:
            msg += (
                f"❌ **{account}** "
                f"(`#{channel_name}` / "
                f"shift `{shift_text}`) "
                f"— nobody ready for this shift\n"
            )

    for chunk in sendable_chunks(
        msg
    ):
        await ctx.send(
            chunk
        )


@bot.command()
async def reloadschedule(ctx):
    global schedule_cache

    schedule_cache = (
        load_schedule_from_csv()
    )

    clock_data[
        "reminded"
    ] = {}

    clock_data[
        "no_clockin_alerts"
    ] = {}

    save_clock_data()

    await ctx.send(
        f"✅ Schedule reloaded "
        f"and reminders reset. "
        f"Shifts loaded: "
        f"**{len(schedule_cache)}**"
    )


@bot.command(name="setstreak")
async def setstreak(
    ctx,
    *,
    args: str = None
):
    if not can_manage_streaks(
        ctx.author
    ):
        await ctx.send(
            "❌ You don't have permission "
            "to set streaks."
        )
        return

    if not args:
        await ctx.send(
            "Use it like this: "
            "`!setstreak username 12`"
        )
        return

    try:
        (
            user_identifier,
            streak_text
        ) = args.rsplit(
            " ",
            1
        )

        streak_value = int(
            streak_text
        )

    except ValueError:
        await ctx.send(
            "Use it like this: "
            "`!setstreak username 12`"
        )
        return

    if streak_value < 0:
        await ctx.send(
            "❌ Streak cannot be negative."
        )
        return

    guild = get_main_guild(ctx)

    if guild is None:
        await ctx.send(
            "❌ Guild not found. "
            "Check GUILD_ID."
        )
        return

    member = find_member_by_identifier(
        guild,
        user_identifier
    )

    if member is None:
        await ctx.send(
            "❌ User not found. "
            "Use their Discord username, "
            "display name, mention, "
            "or Discord ID."
        )
        return

    today = datetime.now(
        TZ
    ).strftime(
        "%Y-%m-%d"
    )

    now_text = datetime.now(
        TZ
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    streak_data = (
        get_or_create_streak_data(
            member
        )
    )

    old_streak = streak_data.get(
        "streak",
        0
    )

    last_clockin_channel_name = (
        streak_data.get(
            "last_clockin_channel"
        )
    )

    last_clockin_guild_id = (
        streak_data.get(
            "last_clockin_guild_id"
        )
    )

    streak_data[
        "streak"
    ] = streak_value

    streak_data[
        "last_counted_date"
    ] = today

    streak_data[
        "last_reset_date"
    ] = None

    streak_data[
        "first_clockin_date"
    ] = today

    streak_data[
        "first_clockin_status"
    ] = "manual_set"

    streak_data[
        "first_clockin_channel"
    ] = (
        f"manual:"
        f"{ctx.channel.name}"
    )

    streak_data[
        "first_clockin_time"
    ] = now_text

    streak_data[
        "manual_set_date"
    ] = now_text

    streak_data[
        "manual_set_by"
    ] = ctx.author.name

    streak_data[
        "manual_set_channel"
    ] = ctx.channel.name

    save_clock_data()

    await ctx.send(
        f"✅ Streak for "
        f"**{member.display_name}** "
        f"has been set from "
        f"**{old_streak}** "
        f"to **{streak_value}**."
    )

    notification_guild = guild

    if (
        last_clockin_guild_id
        and str(
            last_clockin_guild_id
        ).isdigit()
    ):
        saved_guild = bot.get_guild(
            int(
                last_clockin_guild_id
            )
        )

        if saved_guild is not None:
            notification_guild = (
                saved_guild
            )

    if last_clockin_channel_name:
        last_clockin_channel = (
            find_channel_by_name(
                notification_guild,
                last_clockin_channel_name
            )
        )

        if (
            last_clockin_channel
            is not None
        ):
            await last_clockin_channel.send(
                f"🔥 Streak update for "
                f"**{member.display_name}**: "
                f"your streak has been set "
                f"to **{streak_value}**."
            )

        else:
            await ctx.send(
                f"⚠️ Streak was set, "
                f"but I could not find "
                f"the last clock-in channel: "
                f"`{last_clockin_channel_name}`"
            )

    else:
        await ctx.send(
            "⚠️ Streak was set, "
            "but this user has no saved "
            "last clock-in channel yet."
        )


@bot.command(
    name="contentrequest",
    aliases=[
        "cr",
        "needcontent"
    ]
)
async def contentrequest(
    ctx,
    *,
    request_text: str = None
):
    current_channel_name = (
        normalize_name(
            ctx.channel.name
        )
    )

    if (
        "customs"
        not in current_channel_name
    ):
        await ctx.send(
            "❌ Content requests must be "
            "submitted in the model's "
            "**customs** channel."
        )
        return

    if not request_text:
        await ctx.send(
            "Use it like this: "
            "`!contentrequest "
            "need new SFW selfies for wall`"
        )
        return

    guild = get_main_guild(ctx)

    if guild is None:
        await ctx.send(
            "❌ Guild not found. "
            "Check GUILD_ID."
        )
        return

    content_channel = (
        find_channel_by_name(
            guild,
            CONTENT_REQUEST_CHANNEL_NAME
        )
    )

    if content_channel is None:
        await ctx.send(
            f"❌ Content request channel "
            f"not found: "
            f"`{CONTENT_REQUEST_CHANNEL_NAME}`"
        )
        return

    model_name = (
        find_account_by_source_channel(
            ctx.channel.name
        )
    )

    content_embed = discord.Embed(
        title="📸 Content Request",
        color=discord.Color.gold(),
        timestamp=datetime.now(TZ)
    )

    content_embed.add_field(
        name="Model",
        value=model_name,
        inline=True
    )

    content_embed.add_field(
        name="Requested by",
        value=ctx.author.display_name,
        inline=True
    )

    content_embed.add_field(
        name="Source",
        value=f"#{ctx.channel.name}",
        inline=True
    )

    content_embed.add_field(
        name="Request",
        value=request_text,
        inline=False
    )

    content_embed.add_field(
        name="Status",
        value=(
            "👀 Reviewing | "
            "✅ Done | "
            "❌ Declined"
        ),
        inline=False
    )

    sent_message = (
        await content_channel.send(
            embed=content_embed
        )
    )

    for emoji in [
        "👀",
        "✅",
        "❌"
    ]:
        try:
            await sent_message.add_reaction(
                emoji
            )

        except Exception as e:
            print(
                f"Failed to add "
                f"content request reaction "
                f"{emoji}: {e}"
            )

    await ctx.message.add_reaction(
        "✅"
    )

    await ctx.send(
        f"✅ Content request sent "
        f"for **{model_name}**"
    )


# =========================
# MMA COMMAND
# =========================

@bot.command(name="mma")
async def mma(
    ctx,
    *,
    mass_message: str = None
):
    current_channel_name = (
        normalize_name(
            ctx.channel.name
        )
    )

    if (
        "approval"
        not in current_channel_name
    ):
        await ctx.send(
            "❌ Mass message approvals must "
            "be submitted in a channel that "
            "has **approval** in its name."
        )
        return

    if not mass_message:
        await ctx.send(
            "Use it like this:\n"
            "`!mma your mass message here`"
        )
        return

    if len(mass_message) > 4000:
        await ctx.send(
            "❌ Mass message is too long "
            "for the approval system."
        )
        return

    guild = get_main_guild(ctx)

    if guild is None:
        await ctx.send(
            "❌ Guild not found. "
            "Check GUILD_ID."
        )
        return

    approval_channel = (
        find_channel_by_name(
            guild,
            APPROVAL_CHANNEL_NAME
        )
    )

    if approval_channel is None:
        await ctx.send(
            f"❌ Approval channel "
            f"not found: "
            f"`#{APPROVAL_CHANNEL_NAME}`"
        )
        return

    request_id = (
        create_mma_request_id()
    )

    attachments = [
        attachment.url
        for attachment
        in ctx.message.attachments
    ]

    model_name = (
        find_account_by_source_channel(
            ctx.channel.name
        )
    )

    request_data = {
        "request_id": request_id,
        "requester_id": str(
            ctx.author.id
        ),
        "requester_name": (
            ctx.author.display_name
        ),
        "guild_id": str(
            guild.id
        ),
        "source_channel_id": str(
            ctx.channel.id
        ),
        "source_channel_name": (
            ctx.channel.name
        ),
        "source_message_id": str(
            ctx.message.id
        ),
        "model_name": model_name,
        "mass_message": mass_message,
        "attachments": attachments,
        "status": "pending",
        "submitted_at": datetime.now(
            TZ
        ).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "approval_channel_id": str(
            approval_channel.id
        ),
        "approval_message_id": None,
        "decided_by": None,
        "decided_by_name": None,
        "decided_at": None
    }

    if (
        "mma_requests"
        not in clock_data
    ):
        clock_data[
            "mma_requests"
        ] = {}

    clock_data[
        "mma_requests"
    ][request_id] = request_data

    save_clock_data()

    embed = build_mma_embed(
        request_data
    )

    view = MMAApprovalView(
        request_id
    )

    try:
        approval_message = (
            await approval_channel.send(
                embed=embed,
                view=view
            )
        )

        request_data[
            "approval_message_id"
        ] = str(
            approval_message.id
        )

        save_clock_data()

    except Exception as e:
        print(
            f"Failed to send "
            f"MMA approval: {e}"
        )

        clock_data[
            "mma_requests"
        ].pop(
            request_id,
            None
        )

        save_clock_data()

        await ctx.send(
            "❌ Failed to send "
            "the mass message "
            "for approval."
        )
        return

    await ctx.message.add_reaction(
        "✅"
    )

    await ctx.send(
        f"📨 {ctx.author.mention} "
        f"your mass message has been "
        f"sent for approval."
    )


@bot.command()
async def announcement(
    ctx,
    *,
    message: str = None
):
    if not message:
        await ctx.send(
            "Please write the announcement "
            "after the command."
        )
        return

    guild = get_main_guild(ctx)

    if guild is None:
        await ctx.send(
            "❌ Guild not found. "
            "Check GUILD_ID."
        )
        return

    announcement_channels = (
        get_unique_announcement_channels()
    )

    if not announcement_channels:
        await ctx.send(
            "❌ No announcement channels "
            "found in the sheet. "
            "Add `announcement_channel_name` "
            "column first."
        )
        return

    batch_id = (
        create_announcement_batch_id()
    )

    announcement_text = (
        f"📢 **ANNOUNCEMENT**\n\n"
        f"{message}"
    )

    if len(announcement_text) > 2000:
        await ctx.send(
            "❌ Announcement is too long. "
            "Discord limit is 2000 characters."
        )
        return

    sent_channels = []
    failed_channels = []
    sent_messages = []

    for channel_name in (
        announcement_channels
    ):
        channel = find_channel_by_name(
            guild,
            channel_name
        )

        if channel is None:
            failed_channels.append(
                channel_name
            )
            continue

        try:
            sent_message = (
                await channel.send(
                    announcement_text
                )
            )

            sent_channels.append(
                channel_name
            )

            sent_messages.append({
                "channel_name": (
                    channel_name
                ),
                "channel_id": (
                    sent_message.channel.id
                ),
                "message_id": (
                    sent_message.id
                )
            })

        except Exception as e:
            print(
                f"Failed to send "
                f"announcement to "
                f"{channel_name}: {e}"
            )

            failed_channels.append(
                channel_name
            )

    if sent_messages:
        batch = {
            "batch_id": batch_id,
            "created_at": datetime.now(
                TZ
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "created_by": (
                ctx.author.name
            ),
            "text_preview": (
                message[:150]
            ),
            "messages": sent_messages,
            "deleted": False,
            "type": "announcement"
        }

        clock_data[
            "announcement_batches"
        ].append(
            batch
        )

        clock_data[
            "announcement_batches"
        ] = (
            clock_data[
                "announcement_batches"
            ][-50:]
        )

        save_clock_data()

    reply = ""

    if sent_channels:
        reply += (
            f"✅ Announcement sent "
            f"to **{len(sent_channels)}** "
            f"channels.\n\n"
        )

        reply += (
            f"**Batch ID:** "
            f"`{batch_id}`\n\n"
        )

        reply += (
            "To delete this announcement:\n"
        )

        reply += (
            f"`!deleteannouncement "
            f"{batch_id}`\n"
        )

        reply += "or\n"

        reply += (
            "`!deleteannouncement latest`"
            "\n\n"
        )

        reply += "**Sent to:**\n"

        reply += "\n".join(
            [
                f"- #{name}"
                for name
                in sent_channels
            ]
        )

    if failed_channels:
        if reply:
            reply += "\n\n"

        reply += (
            "❌ Failed / not found:\n"
        )

        reply += "\n".join(
            [
                f"- #{name}"
                for name
                in failed_channels
            ]
        )

    if not reply:
        reply = (
            "❌ Announcement was not "
            "sent to any channel."
        )

    await ctx.send(
        reply
    )


@bot.command(name="rules")
async def rules(
    ctx,
    *,
    message: str = None
):
    if not message:
        await ctx.send(
            "Please write the rules message "
            "after the command."
        )
        return

    guild = get_main_guild(ctx)

    if guild is None:
        await ctx.send(
            "❌ Guild not found. "
            "Check GUILD_ID."
        )
        return

    rules_channels = (
        get_unique_rules_channels(
            guild
        )
    )

    if not rules_channels:
        await ctx.send(
            "❌ No rules channels found. "
            "Make sure model rules channels "
            "have `rules` in the channel name."
        )
        return

    batch_id = (
        create_announcement_batch_id()
    )

    rules_text = message

    if len(rules_text) > 2000:
        await ctx.send(
            "❌ Rules message is too long. "
            "Discord limit is 2000 characters."
        )
        return

    sent_channels = []
    failed_channels = []
    sent_messages = []

    for channel_name in rules_channels:
        channel = find_channel_by_name(
            guild,
            channel_name
        )

        if channel is None:
            failed_channels.append(
                channel_name
            )
            continue

        try:
            sent_message = (
                await channel.send(
                    rules_text
                )
            )

            sent_channels.append(
                channel_name
            )

            sent_messages.append({
                "channel_name": (
                    channel_name
                ),
                "channel_id": (
                    sent_message.channel.id
                ),
                "message_id": (
                    sent_message.id
                )
            })

        except Exception as e:
            print(
                f"Failed to send rules "
                f"message to "
                f"{channel_name}: {e}"
            )

            failed_channels.append(
                channel_name
            )

    if sent_messages:
        batch = {
            "batch_id": batch_id,
            "created_at": datetime.now(
                TZ
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "created_by": (
                ctx.author.name
            ),
            "text_preview": (
                message[:150]
            ),
            "messages": (
                sent_messages
            ),
            "deleted": False,
            "type": "rules"
        }

        clock_data[
            "announcement_batches"
        ].append(
            batch
        )

        clock_data[
            "announcement_batches"
        ] = (
            clock_data[
                "announcement_batches"
            ][-50:]
        )

        save_clock_data()

    reply = ""

    if sent_channels:
        reply += (
            f"✅ Rules message sent "
            f"to **{len(sent_channels)}** "
            f"rules channels.\n\n"
        )

        reply += (
            f"**Batch ID:** "
            f"`{batch_id}`\n\n"
        )

        reply += (
            "To delete this rules message:\n"
        )

        reply += (
            f"`!deleteannouncement "
            f"{batch_id}`\n"
        )

        reply += "or\n"

        reply += (
            "`!deleteannouncement latest`"
            "\n\n"
        )

        reply += "**Sent to:**\n"

        reply += "\n".join(
            [
                f"- #{name}"
                for name
                in sent_channels
            ]
        )

    if failed_channels:
        if reply:
            reply += "\n\n"

        reply += (
            "❌ Failed / not found:\n"
        )

        reply += "\n".join(
            [
                f"- #{name}"
                for name
                in failed_channels
            ]
        )

    if not reply:
        reply = (
            "❌ Rules message was not "
            "sent to any channel."
        )

    await ctx.send(
        reply
    )


@bot.command(
    name="training",
    aliases=[
        "trainings",
        "trainingmaterial",
        "tm"
    ]
)
async def training(
    ctx,
    *,
    message: str = None
):
    if not can_manage_broadcasts(
        ctx.author
    ):
        await ctx.send(
            "❌ You don't have permission "
            "to send training material "
            "to all channels."
        )
        return

    guild = get_main_guild(ctx)

    if guild is None:
        await ctx.send(
            "❌ Guild not found. "
            "Check GUILD_ID."
        )
        return

    training_channels = (
        get_unique_training_channels(
            guild
        )
    )

    if not training_channels:
        await ctx.send(
            "❌ No training channels found. "
            "Make sure training channels "
            "have `training` in the "
            "channel name."
        )
        return

    referenced_message = None

    if (
        ctx.message.reference
        and ctx.message.reference.message_id
    ):
        try:
            reference_channel = (
                ctx.channel
            )

            if (
                ctx.message.reference.channel_id
            ):
                found_channel = (
                    bot.get_channel(
                        ctx.message.reference.channel_id
                    )
                )

                if (
                    found_channel
                    is not None
                ):
                    reference_channel = (
                        found_channel
                    )

            referenced_message = (
                await reference_channel.fetch_message(
                    ctx.message.reference.message_id
                )
            )

        except Exception as e:
            print(
                f"Failed to fetch "
                f"referenced training "
                f"message: {e}"
            )

            referenced_message = None

    source_attachments = (
        ctx.message.attachments
    )

    training_text = (
        message
        or ""
    )

    if referenced_message is not None:
        if (
            not source_attachments
            and referenced_message.attachments
        ):
            source_attachments = (
                referenced_message.attachments
            )

        if (
            not training_text
            and referenced_message.content
        ):
            training_text = (
                referenced_message.content
            )

    if (
        not training_text
        and not source_attachments
    ):
        await ctx.send(
            "Use it like this:\n"
            "`!training message`\n"
            "or upload a photo/video "
            "with `!training`\n"
            "or reply to a training "
            "message/photo/video "
            "with `!training`."
        )
        return

    if len(training_text) > 2000:
        await ctx.send(
            "❌ Training message is "
            "too long. Discord limit "
            "is 2000 characters."
        )
        return

    batch_id = (
        create_announcement_batch_id()
    )

    sent_channels = []
    failed_channels = []
    sent_messages = []

    for channel_name in (
        training_channels
    ):
        channel = find_channel_by_name(
            guild,
            channel_name
        )

        if channel is None:
            failed_channels.append(
                channel_name
            )
            continue

        try:
            files = []

            for attachment in (
                source_attachments
            ):
                files.append(
                    await attachment.to_file()
                )

            if files:
                sent_message = (
                    await channel.send(
                        content=(
                            training_text
                            if training_text
                            else None
                        ),
                        files=files
                    )
                )

            else:
                sent_message = (
                    await channel.send(
                        training_text
                    )
                )

            for emoji in [
                "👀",
                "✅",
                "❌"
            ]:
                try:
                    await sent_message.add_reaction(
                        emoji
                    )

                except Exception as e:
                    print(
                        f"Failed to add "
                        f"training reaction "
                        f"{emoji}: {e}"
                    )

            sent_channels.append(
                channel_name
            )

            sent_messages.append({
                "channel_name": (
                    channel_name
                ),
                "channel_id": (
                    sent_message.channel.id
                ),
                "message_id": (
                    sent_message.id
                )
            })

        except Exception as e:
            print(
                f"Failed to send "
                f"training material "
                f"to {channel_name}: {e}"
            )

            failed_channels.append(
                channel_name
            )

    if sent_messages:
        batch = {
            "batch_id": batch_id,
            "created_at": datetime.now(
                TZ
            ).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "created_by": (
                ctx.author.name
            ),
            "text_preview": (
                training_text[:150]
                if training_text
                else "Training attachment"
            ),
            "messages": (
                sent_messages
            ),
            "deleted": False,
            "type": "training"
        }

        clock_data[
            "announcement_batches"
        ].append(
            batch
        )

        clock_data[
            "announcement_batches"
        ] = (
            clock_data[
                "announcement_batches"
            ][-50:]
        )

        save_clock_data()

    reply = ""

    if sent_channels:
        reply += (
            f"✅ Training material sent "
            f"to **{len(sent_channels)}** "
            f"training channels.\n\n"
        )

        reply += (
            f"**Batch ID:** "
            f"`{batch_id}`\n\n"
        )

        reply += (
            "To delete this "
            "training material:\n"
        )

        reply += (
            f"`!deletetraining "
            f"{batch_id}`\n"
        )

        reply += "or\n"

        reply += (
            "`!deletetraining latest`"
            "\n\n"
        )

        reply += "**Sent to:**\n"

        reply += "\n".join(
            [
                f"- #{name}"
                for name
                in sent_channels
            ]
        )

    if failed_channels:
        if reply:
            reply += "\n\n"

        reply += (
            "❌ Failed / not found:\n"
        )

        reply += "\n".join(
            [
                f"- #{name}"
                for name
                in failed_channels
            ]
        )

    if not reply:
        reply = (
            "❌ Training material "
            "was not sent to "
            "any channel."
        )

    await ctx.send(
        reply
    )


@bot.command(
    name="trainingchannels"
)
async def trainingchannels(ctx):
    guild = get_main_guild(ctx)

    if guild is None:
        await ctx.send(
            "❌ Guild not found. "
            "Check GUILD_ID."
        )
        return

    channels = (
        get_unique_training_channels(
            guild
        )
    )

    if not channels:
        await ctx.send(
            "No training channels found."
        )
        return

    msg = (
        "**Training channels found:**\n"
    )

    msg += "\n".join(
        [
            f"- #{channel}"
            for channel in channels
        ]
    )

    for chunk in sendable_chunks(
        msg
    ):
        await ctx.send(
            chunk
        )


@bot.command()
async def announcements(ctx):
    batches = clock_data.get(
        "announcement_batches",
        []
    )

    active_batches = [
        batch
        for batch in batches
        if not batch.get(
            "deleted",
            False
        )
    ]

    if not active_batches:
        await ctx.send(
            "No active announcements found."
        )
        return

    last_batches = (
        active_batches[-10:]
    )

    last_batches.reverse()

    msg = (
        "**Recent active announcements / "
        "rules / training:**\n\n"
    )

    for batch in last_batches:
        batch_id = batch.get(
            "batch_id",
            "unknown"
        )

        created_at = batch.get(
            "created_at",
            "unknown time"
        )

        created_by = batch.get(
            "created_by",
            "unknown"
        )

        text_preview = batch.get(
            "text_preview",
            ""
        )

        channel_count = len(
            batch.get(
                "messages",
                []
            )
        )

        batch_type = batch.get(
            "type",
            "announcement"
        )

        msg += (
            f"**{batch_id}** "
            f"— `{batch_type}`\n"
        )

        msg += (
            f"Created: "
            f"`{created_at}` by "
            f"**{created_by}**\n"
        )

        msg += (
            f"Channels: "
            f"**{channel_count}**\n"
        )

        msg += (
            f"Preview: "
            f"{text_preview}\n"
        )

        if batch_type == "training":
            msg += (
                f"Delete: "
                f"`!deletetraining "
                f"{batch_id}`\n\n"
            )

        else:
            msg += (
                f"Delete: "
                f"`!deleteannouncement "
                f"{batch_id}`\n\n"
            )

    for chunk in sendable_chunks(
        msg
    ):
        await ctx.send(
            chunk
        )


@bot.command()
async def deleteannouncement(
    ctx,
    batch_id: str = None
):
    if not batch_id:
        await ctx.send(
            "Please write which "
            "announcement to delete.\n\n"
            "Examples:\n"
            "`!deleteannouncement latest`\n"
            "`!deleteannouncement "
            "20260620-123456`"
        )
        return

    guild = get_main_guild(ctx)

    if guild is None:
        await ctx.send(
            "❌ Guild not found. "
            "Check GUILD_ID."
        )
        return

    batch = None

    if batch_id.lower() == "latest":
        batch = (
            get_latest_active_announcement_batch()
        )

    else:
        for saved_batch in (
            clock_data.get(
                "announcement_batches",
                []
            )
        ):
            if (
                saved_batch.get(
                    "batch_id"
                )
                == batch_id
            ):
                batch = saved_batch
                break

    if batch is None:
        await ctx.send(
            "❌ Announcement batch "
            "not found."
        )
        return

    if batch.get(
        "deleted",
        False
    ):
        await ctx.send(
            "This announcement batch "
            "was already marked as deleted."
        )
        return

    await delete_saved_batch_messages(
        ctx,
        batch,
        "announcement"
    )


@bot.command(
    name="deletetraining"
)
async def deletetraining(
    ctx,
    batch_id: str = None
):
    if not can_manage_broadcasts(
        ctx.author
    ):
        await ctx.send(
            "❌ You don't have permission "
            "to delete training material."
        )
        return

    if not batch_id:
        await ctx.send(
            "Please write which training "
            "material to delete.\n\n"
            "Examples:\n"
            "`!deletetraining latest`\n"
            "`!deletetraining "
            "20260620-123456`"
        )
        return

    guild = get_main_guild(ctx)

    if guild is None:
        await ctx.send(
            "❌ Guild not found. "
            "Check GUILD_ID."
        )
        return

    batch = None

    if batch_id.lower() == "latest":
        batch = (
            get_latest_active_batch_by_type(
                "training"
            )
        )

    else:
        for saved_batch in (
            clock_data.get(
                "announcement_batches",
                []
            )
        ):
            if (
                saved_batch.get(
                    "batch_id"
                )
                == batch_id
                and saved_batch.get(
                    "type"
                )
                == "training"
            ):
                batch = saved_batch
                break

    if batch is None:
        await ctx.send(
            "❌ Training batch not found."
        )
        return

    if batch.get(
        "deleted",
        False
    ):
        await ctx.send(
            "This training batch "
            "was already marked "
            "as deleted."
        )
        return

    await delete_saved_batch_messages(
        ctx,
        batch,
        "training"
    )


@bot.command()
async def announcementchannels(ctx):
    channels = (
        get_unique_announcement_channels()
    )

    if not channels:
        await ctx.send(
            "No announcement channels "
            "found in the sheet."
        )
        return

    msg = (
        "**Announcement channels "
        "from sheet:**\n"
    )

    msg += "\n".join(
        [
            f"- #{channel}"
            for channel in channels
        ]
    )

    for chunk in sendable_chunks(
        msg
    ):
        await ctx.send(
            chunk
        )


@bot.command()
async def farm(
    ctx,
    fan_id: str = None,
    amount: str = None
):
    current_channel_name = (
        normalize_name(
            ctx.channel.name
        )
    )

    if (
        "staff"
        not in current_channel_name
    ):
        await ctx.send(
            "❌ Farm logs must be submitted "
            "in the model's **staff** channel."
        )
        return

    if not fan_id or not amount:
        await ctx.send(
            "Use it like this: "
            "`!farm u32475632407 3K`"
        )
        return

    guild = get_main_guild(ctx)

    if guild is None:
        await ctx.send(
            "❌ Guild not found. "
            "Check GUILD_ID."
        )
        return

    if not FARM_REPORT_CHANNEL_NAME:
        await ctx.send(
            "❌ FARM_REPORT_CHANNEL_NAME "
            "is not set in `.env`."
        )
        return

    report_channel = (
        find_channel_by_name(
            guild,
            FARM_REPORT_CHANNEL_NAME
        )
    )

    if report_channel is None:
        await ctx.send(
            f"❌ Farm report channel "
            f"not found: "
            f"`{FARM_REPORT_CHANNEL_NAME}`"
        )
        return

    model_name = (
        find_account_by_source_channel(
            ctx.channel.name
        )
    )

    chatter_name = (
        ctx.author.display_name
    )

    farm_embed = discord.Embed(
        title=(
            f"🌽 {amount.upper()} "
            f"FARM LOGGED"
        ),
        description=(
            f"👤 **{model_name}** | "
            f"💬 **{chatter_name}** | "
            f"🆔 `{fan_id}`"
        ),
        color=discord.Color.gold(),
        timestamp=datetime.now(TZ)
    )

    farm_embed.set_footer(
        text=(
            f"Logged from "
            f"#{ctx.channel.name}"
        )
    )

    await report_channel.send(
        embed=farm_embed
    )

    confirmation_message = (
        await ctx.send(
            f"✅ Farm logged for "
            f"**{model_name}**.\n"
            f"Make sure to react with ✅ "
            f"once the notes on the fan "
            f"are updated."
        )
    )

    for emoji in [
        "🌽",
        "✅",
        "❌"
    ]:
        try:
            await confirmation_message.add_reaction(
                emoji
            )

        except discord.Forbidden:
            print(
                f"Missing permission "
                f"to add reaction "
                f"{emoji} in "
                f"#{ctx.channel.name}"
            )

        except Exception as e:
            print(
                f"Failed to add reaction "
                f"{emoji}: {e}"
            )


@bot.command()
async def checkreminders(ctx):
    now = datetime.now(TZ)

    today = now.strftime(
        "%Y-%m-%d"
    )

    guild = get_main_guild(ctx)

    if guild is None:
        await ctx.send(
            "❌ Guild not found. "
            "GUILD_ID is wrong."
        )
        return

    checked = 0
    possible = 0
    guild_id = guild.id

    for shift in schedule_cache:
        checked += 1

        try:
            shift_datetime = (
                datetime.strptime(
                    f"{today} "
                    f"{shift['shift_time']}",
                    "%Y-%m-%d %H:%M"
                ).replace(
                    tzinfo=TZ
                )
            )

        except ValueError:
            await ctx.send(
                f"❌ Invalid shift time: "
                f"`{shift['shift_time']}` "
                f"for `{shift['account']}`"
            )
            continue

        reminder_time = (
            shift_datetime
            - timedelta(
                minutes=10
            )
        )

        scheduled_username = (
            shift[
                "scheduled_chatter_username"
            ]
        )

        channel_name = normalize_name(
            shift[
                "channel_name"
            ]
        )

        reminder_key = (
            f"{guild_id}-"
            f"{today}-"
            f"{shift['account']}-"
            f"{channel_name}-"
            f"{shift['shift_time']}"
        )

        someone_clocked_in_for_this_channel = (
            is_channel_clocked_in(
                guild_id,
                channel_name
            )
        )

        should_warn = (
            reminder_time
            <= now
            < shift_datetime
            and not (
                someone_clocked_in_for_this_channel
            )
            and reminder_key
            not in clock_data[
                "reminded"
            ]
        )

        print(
            f"MANUAL CHECK | "
            f"account={shift['account']} | "
            f"channel={channel_name} | "
            f"shift="
            f"{shift_datetime.strftime('%H:%M')} | "
            f"now="
            f"{now.strftime('%H:%M')} | "
            f"reminder="
            f"{reminder_time.strftime('%H:%M')} | "
            f"clocked_in="
            f"{someone_clocked_in_for_this_channel} | "
            f"already_reminded="
            f"{reminder_key in clock_data['reminded']} | "
            f"should_warn="
            f"{should_warn}"
        )

        if should_warn:
            possible += 1

            channel = (
                find_channel_by_name(
                    guild,
                    channel_name
                )
            )

            member = (
                find_member_by_username(
                    guild,
                    scheduled_username
                )
            )

            supervisor_role = (
                find_role_by_name(
                    guild,
                    shift[
                        "supervisor_role_name"
                    ]
                )
            )

            if channel is None:
                await ctx.send(
                    f"❌ Channel not found: "
                    f"`{channel_name}`"
                )
                continue

            if member is None:
                await ctx.send(
                    f"❌ Member not found: "
                    f"`{scheduled_username}`"
                )
                continue

            if supervisor_role is None:
                await ctx.send(
                    f"❌ Supervisor role "
                    f"not found: "
                    f"`{shift['supervisor_role_name']}`"
                )
                continue

            await channel.send(
                f"⏰ {member.mention} "
                f"your shift starts in "
                f"**10 minutes** and nobody "
                f"is clocked in for this "
                f"account yet.\n\n"
                f"**Account:** "
                f"{shift['account']}\n"
                f"{supervisor_role.mention} "
                f"please check this."
            )

            clock_data[
                "reminded"
            ][reminder_key] = True

            save_clock_data()

            await ctx.send(
                f"✅ Sent reminder for "
                f"`{shift['account']}` "
                f"in `#{channel_name}`"
            )

    if possible == 0:
        await ctx.send(
            f"Checked **{checked} shifts**. "
            f"No reminders are due "
            f"right now.\n"
            f"Current bot time: "
            f"**{now.strftime('%H:%M')}**"
        )


@tasks.loop(minutes=1)
async def shift_reminder_loop():
    now = datetime.now(TZ)

    today = now.strftime(
        "%Y-%m-%d"
    )

    guild = get_main_guild()

    if guild is None:
        print(
            "Guild not found."
        )
        return

    guild_id = guild.id

    for shift in schedule_cache:
        try:
            shift_datetime = (
                datetime.strptime(
                    f"{today} "
                    f"{shift['shift_time']}",
                    "%Y-%m-%d %H:%M"
                ).replace(
                    tzinfo=TZ
                )
            )

        except ValueError:
            print(
                f"Invalid shift time: "
                f"{shift['shift_time']} "
                f"for {shift['account']}"
            )
            continue

        reminder_time = (
            shift_datetime
            - timedelta(
                minutes=10
            )
        )

        no_clockin_alert_time = (
            shift_datetime
            + timedelta(
                minutes=(
                    NO_CLOCKIN_ALERT_AFTER_MINUTES
                )
            )
        )

        no_clockin_alert_window_end = (
            shift_datetime
            + timedelta(
                minutes=(
                    NO_CLOCKIN_ALERT_AFTER_MINUTES
                    + NO_CLOCKIN_ALERT_WINDOW_MINUTES
                )
            )
        )

        scheduled_username = (
            shift[
                "scheduled_chatter_username"
            ]
        )

        channel_name = normalize_name(
            shift[
                "channel_name"
            ]
        )

        reminder_key = (
            f"{guild_id}-"
            f"{today}-"
            f"{shift['account']}-"
            f"{channel_name}-"
            f"{shift['shift_time']}"
        )

        no_clockin_alert_key = (
            f"{guild_id}-"
            f"{today}-"
            f"{shift['account']}-"
            f"{channel_name}-"
            f"{shift['shift_time']}-"
            f"no-clockin"
        )

        someone_clocked_in_for_this_channel = (
            is_channel_clocked_in(
                guild_id,
                channel_name
            )
        )

        should_warn = (
            reminder_time
            <= now
            < shift_datetime
            and not (
                someone_clocked_in_for_this_channel
            )
            and reminder_key
            not in clock_data[
                "reminded"
            ]
        )

        should_send_no_clockin_alert = (
            no_clockin_alert_time
            <= now
            <= no_clockin_alert_window_end
            and not (
                someone_clocked_in_for_this_channel
            )
            and no_clockin_alert_key
            not in clock_data[
                "no_clockin_alerts"
            ]
        )

        print(
            f"CHECK | "
            f"account={shift['account']} | "
            f"channel={channel_name} | "
            f"shift="
            f"{shift_datetime.strftime('%H:%M')} | "
            f"now="
            f"{now.strftime('%H:%M')} | "
            f"reminder="
            f"{reminder_time.strftime('%H:%M')} | "
            f"no_clockin_alert="
            f"{no_clockin_alert_time.strftime('%H:%M')} | "
            f"no_clockin_window_end="
            f"{no_clockin_alert_window_end.strftime('%H:%M')} | "
            f"clocked_in="
            f"{someone_clocked_in_for_this_channel} | "
            f"already_reminded="
            f"{reminder_key in clock_data['reminded']} | "
            f"already_no_clockin_alert="
            f"{no_clockin_alert_key in clock_data['no_clockin_alerts']} | "
            f"should_warn="
            f"{should_warn} | "
            f"should_send_no_clockin_alert="
            f"{should_send_no_clockin_alert}"
        )

        if should_warn:
            print(
                f"SENDING REMINDER: "
                f"{shift['account']} | "
                f"{channel_name} | "
                f"{scheduled_username}"
            )

            channel = (
                find_channel_by_name(
                    guild,
                    channel_name
                )
            )

            member = (
                find_member_by_username(
                    guild,
                    scheduled_username
                )
            )

            supervisor_role = (
                find_role_by_name(
                    guild,
                    shift[
                        "supervisor_role_name"
                    ]
                )
            )

            if channel is None:
                print(
                    f"Channel not found: "
                    f"{channel_name}"
                )
                continue

            if member is None:
                print(
                    f"Member not found: "
                    f"{scheduled_username}"
                )
                continue

            if supervisor_role is None:
                print(
                    f"Supervisor role not found: "
                    f"{shift['supervisor_role_name']}"
                )
                continue

            await channel.send(
                f"⏰ {member.mention} "
                f"your shift starts in "
                f"**10 minutes** and nobody "
                f"is clocked in for this "
                f"account yet.\n\n"
                f"**Account:** "
                f"{shift['account']}\n"
                f"{supervisor_role.mention} "
                f"please check this."
            )

            clock_data[
                "reminded"
            ][reminder_key] = True

            save_clock_data()

        if should_send_no_clockin_alert:
            print(
                f"SENDING NO CLOCK-IN ALERT: "
                f"{shift['account']} | "
                f"{channel_name} | "
                f"{scheduled_username}"
            )

            channel = (
                find_channel_by_name(
                    guild,
                    channel_name
                )
            )

            supervisor_role = (
                find_role_by_name(
                    guild,
                    shift[
                        "supervisor_role_name"
                    ]
                )
            )

            if channel is None:
                print(
                    f"Channel not found for "
                    f"no clock-in alert: "
                    f"{channel_name}"
                )
                continue

            clock_data[
                "no_clockin_alerts"
            ][no_clockin_alert_key] = True

            save_clock_data()

            supervisor_text = ""

            if supervisor_role is not None:
                supervisor_text = (
                    f"\n"
                    f"{supervisor_role.mention} "
                    f"please check coverage."
                )

            await channel.send(
                f"🚨 **No clock-in alert**\n\n"
                f"Nobody is clocked in for "
                f"**{shift['account']}** "
                f"**{NO_CLOCKIN_ALERT_AFTER_MINUTES} "
                f"minutes after shift start**."
                f"{supervisor_text}"
            )


bot.run(DISCORD_TOKEN)

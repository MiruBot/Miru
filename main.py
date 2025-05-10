import nextcord
from nextcord.ext import commands
from nextcord import slash_command, SlashOption
from nextcord.ui import Button, View, Modal, TextInput
import yt_dlp
from youtube_search import YoutubeSearch
import asyncio
from dotenv import load_dotenv
import os
import sqlite3
import random
import string
import re
from datetime import datetime
import cachetools  # 새로 추가
from openai import OpenAI   # gpt
import traceback

# 캐시 설정 개선
CACHE_TTL = 3600  # 1시간
CACHE_MAX_SIZE = 1000  # 서버당 최대 캐시 크기
guild_caches = {}  # 서버별 캐시 저장소

class GuildCache:
    def __init__(self):
        self.song_cache = cachetools.TTLCache(maxsize=100, ttl=CACHE_TTL)
        self.url_cache = cachetools.TTLCache(maxsize=100, ttl=CACHE_TTL)
        self.last_accessed = datetime.now()

def get_guild_cache(guild_id: int) -> GuildCache:
    if guild_id not in guild_caches:
        guild_caches[guild_id] = GuildCache()
    guild_caches[guild_id].last_accessed = datetime.now()
    return guild_caches[guild_id]

# 캐시 클린업 작업
async def cleanup_guild_caches():
    while True:
        try:
            current_time = datetime.now()
            inactive_guilds = []
            
            for guild_id, cache in guild_caches.items():
                # 24시간 이상 미사용된 캐시 제거
                if (current_time - cache.last_accessed).total_seconds() > 86400:
                    inactive_guilds.append(guild_id)
            
            for guild_id in inactive_guilds:
                del guild_caches[guild_id]
                
            await asyncio.sleep(3600)  # 1시간마다 체크
        except Exception as e:
            print(f"Cache cleanup error: {e}")
            await asyncio.sleep(3600)

load_dotenv()

intents = nextcord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='미루야', intents=intents)
openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))    # gpt

# 전역 변수들
current_playing = {}  # 현재 재생 중인 노래 정보
search_locks = {}     # 검색 락
voice_states = {}     # 음성 상태
repeat_states = {}    # 반복 재생 상태
shuffle_states = {}   # 셔플 상태

class SearchLock:
    def __init__(self):
        self.is_locked = False
        self.current_user = None

class VoiceState:
    def __init__(self):
        self.timer_task = None
        self.leave_timer = 300  # 5분 타이머

    async def start_timer(self, voice_client, message):
        if self.timer_task:
            self.timer_task.cancel()
        
        self.timer_task = asyncio.create_task(self.timer_callback(voice_client, message))

    async def timer_callback(self, voice_client, message):
        await asyncio.sleep(self.leave_timer)
        if voice_client and voice_client.is_connected():
            channel_members = len([m for m in voice_client.channel.members if not m.bot])
            if channel_members == 0:
                await self.handle_disconnect(voice_client, message)

    async def handle_disconnect(self, voice_client, message):
        try:
            if voice_client and voice_client.is_connected():
                await voice_client.disconnect()
            
            guild_id = message.guild.id
            current_playing.pop(guild_id, None)
            db.clear_guild_queue(guild_id)
            
            try:
                # 메시지가 여전히 존재하는지 확인 (fetch 대신 다른 방법 사용)
                try:
                    # 메시지 존재 확인을 위한 대체 방법
                    channel = message.channel
                    try:
                        # 메시지 ID로 다시 조회
                        updated_message = await channel.fetch_message(message.id)
                        message = updated_message  # 업데이트된 메시지 참조로 교체
                    except nextcord.NotFound:
                        return  # 메시지가 삭제됨
                except Exception as e:
                    print(f"Message check error: {e}")
                    return
                
                # 메시지가 존재하면 업데이트
                await message.edit(
                    embed=nextcord.Embed(
                        title="👋 퇴장",
                        description="미루 나갔어... 다음에 또 불러줘... 🥺",
                        color=nextcord.Color.blue()
                    ),
                    view=InitialView(message)
                )
            except nextcord.HTTPException as e:
                print(f"Failed to edit message: {e}")
                
        except Exception as e:
            print(f"Disconnect error: {e}")
            
        finally:
            # 타이머 정리
            if self.timer_task:
                self.timer_task.cancel()
                self.timer_task = None
    

class VoiceStateWithRetry(VoiceState):
    def __init__(self):
        super().__init__()
        self.retry_count = 0
        self.max_retries = 3
        
    async def connect_with_retry(self, voice_channel):
        self.retry_count = 0
        while self.retry_count < self.max_retries:
            try:
                return await voice_channel.connect(timeout=20.0, reconnect=True)
            except Exception as e:
                self.retry_count += 1
                if self.retry_count >= self.max_retries:
                    raise e
                await asyncio.sleep(1)

def get_search_lock(guild_id: int) -> SearchLock:
    if guild_id not in search_locks:
        search_locks[guild_id] = SearchLock()
    return search_locks[guild_id]

def get_voice_state(guild_id: int) -> VoiceState:
    if guild_id not in voice_states:
        voice_states[guild_id] = VoiceState()
    return voice_states[guild_id]

def get_repeat_state(guild_id: int) -> bool:
    return repeat_states.get(guild_id, False)

def get_shuffle_state(guild_id: int) -> bool:
    return shuffle_states.get(guild_id, False)

def get_current_playing_song(guild_id: int):
    return current_playing.get(guild_id)

def set_current_playing_song(guild_id: int, song_info: dict):
    current_playing[guild_id] = song_info

# YT-DLP 설정
ytdl_format_options = {
    'format': 'bestaudio/best',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'opus',
        'preferredquality': '192',
    }],
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'quiet': True,
    'extract_flat': True,  # 플레이리스트 처리 최적화
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

async def get_audio_source(url: str, guild_id: int):
    """음원 소스를 가져오는 함수 (캐싱 적용)"""
    cache_key = f"source_{url}"
    guild_cache = get_guild_cache(guild_id)
    if cache_key in guild_cache.url_cache:
        return guild_cache.url_cache[cache_key]

    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
        if not data:
            raise Exception("미루는 이 오디오 소스를 찾을 수 없어...")
        
        source = await nextcord.FFmpegOpusAudio.from_probe(data['url'], **FFMPEG_OPTIONS)
        guild_cache.url_cache[cache_key] = source
        return source
    except Exception as e:
        print(f"Error getting audio source: {e}")
        raise

async def get_song_info(url: str, guild_id: int) -> dict:
    """노래 정보를 가져오는 함수 (캐싱 적용)"""
    guild_cache = get_guild_cache(guild_id)
    if url in guild_cache.song_cache:
        return guild_cache.song_cache[url]

    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
        if not data:
            raise Exception("미루는 이 노래 정보를 찾을 수 없어...")
        
        song_info = {
            'url': data['url'],
            'title': data['title'],
            'duration': data.get('duration_string', 'N/A'),
            'channel': data.get('uploader', 'N/A'),
            'thumbnail': data.get('thumbnail')
        }
        guild_cache.song_cache[url] = song_info
        return song_info
    except Exception as e:
        print(f"Error getting song info: {e}")
        raise

class PlayManager:
    """재생 관리 클래스"""
    def __init__(self, bot):
        self.bot = bot
        self.play_locks = {}
    
    async def get_lock(self, guild_id: int):
        if guild_id not in self.play_locks:
            self.play_locks[guild_id] = asyncio.Lock()
        return self.play_locks[guild_id]

    async def play_song(self, voice_client, song_info, guild_id, after_callback):
        try:
            source = await get_audio_source(song_info['url'], guild_id)
            voice_client.play(source, after=after_callback)
            return True
        except Exception as e:
            print(f"Error playing song: {e}")
            return False

play_manager = PlayManager(bot)

class GuildSettings:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.volume = 1.0
        self.dj_role_id = None
        self.max_queue_size = 500
        self.last_updated = datetime.now()

    @classmethod
    def from_db(cls, db_data):
        instance = cls(db_data['guild_id'])
        instance.volume = db_data.get('volume', 1.0)
        instance.dj_role_id = db_data.get('dj_role_id')
        instance.max_queue_size = db_data.get('max_queue_size', 500)
        return instance

class QueueDB:
    def __init__(self):
        self._connection = None
        self._cursor = None
        self.setup()
    
    @property
    def conn(self):
        if self._connection is None:
            self._connection = sqlite3.connect('music_queue.db', 
                isolation_level=None)  # 자동 커밋 모드
            self._connection.row_factory = sqlite3.Row
        return self._connection
    
    @property
    def c(self):
        if self._cursor is None:
            self._cursor = self.conn.cursor()
        return self._cursor

    def setup(self):
        # 현재 재생목록 테이블
        self.c.execute('''
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                duration TEXT,
                channel TEXT,
                thumbnail TEXT,
                position INTEGER NOT NULL
            )
        ''')
        
        # 서버별 설정 테이블
        self.c.execute('''
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                music_channel_id INTEGER
            )
        ''')

        # 저장된 재생목록 테이블
        self.c.execute('''
            CREATE TABLE IF NOT EXISTS saved_queues (
                queue_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                song_count INTEGER NOT NULL
            )
        ''')

        # 저장된 재생목록의 곡 정보
        self.c.execute('''
            CREATE TABLE IF NOT EXISTS saved_queue_songs (
                queue_id TEXT,
                position INTEGER,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                duration TEXT,
                channel TEXT,
                thumbnail TEXT,
                FOREIGN KEY(queue_id) REFERENCES saved_queues(id),
                PRIMARY KEY(queue_id, position)
            )
        ''')
        
        # 음악 플레이어 메시지 저장용 테이블 추가
        self.c.execute('''
            CREATE TABLE IF NOT EXISTS music_players (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL
            )
        ''')
        
        # 서버 설정 테이블 추가
        self.c.execute('''
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                volume REAL DEFAULT 1.0,
                dj_role_id INTEGER,
                max_queue_size INTEGER DEFAULT 500,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        self.conn.commit()
        self.clear_all_queues()
    def add_to_queue(self, guild_id: int, song_info: dict):
        self.c.execute('SELECT MAX(position) FROM queue WHERE guild_id = ?', (guild_id,))
        max_position = self.c.fetchone()[0] or 0
        next_position = max_position + 1

        self.c.execute('''
            INSERT INTO queue (guild_id, url, title, duration, channel, thumbnail, position)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            guild_id,
            song_info['url'],
            song_info['title'],
            song_info.get('duration', 'N/A'),
            song_info.get('channel', 'N/A'),
            song_info.get('thumbnail'),
            next_position
        ))
        self.conn.commit()
        return next_position

    def get_queue(self, guild_id: int):
        self.c.execute('''
            SELECT url, title, duration, channel, thumbnail, position
            FROM queue
            WHERE guild_id = ?
            ORDER BY position ASC
        ''', (guild_id,))
        return [
            {
                'url': row[0],
                'title': row[1],
                'duration': row[2],
                'channel': row[3],
                'thumbnail': row[4],
                'position': row[5]
            }
            for row in self.c.fetchall()
        ]

    def get_next_song(self, guild_id: int):
        self.c.execute('''
            SELECT url, title, duration, channel, thumbnail, position
            FROM queue
            WHERE guild_id = ?
            ORDER BY position ASC
            LIMIT 1
        ''', (guild_id,))
        row = self.c.fetchone()
        if row:
            song = {
                'url': row[0],
                'title': row[1],
                'duration': row[2],
                'channel': row[3],
                'thumbnail': row[4],
                'position': row[5]
            }
            self.remove_from_queue(guild_id, row[5])
            return song
        return None

    def remove_from_queue(self, guild_id: int, position: int):
        self.c.execute('''
            DELETE FROM queue
            WHERE guild_id = ? AND position = ?
        ''', (guild_id, position))
        
        self.c.execute('''
            UPDATE queue
            SET position = position - 1
            WHERE guild_id = ? AND position > ?
        ''', (guild_id, position))
        self.conn.commit()
        
        self.c.execute('SELECT COUNT(*) FROM queue WHERE guild_id = ?', (guild_id,))
        remaining_songs = self.c.fetchone()[0]
        return remaining_songs > 0

    def clear_guild_queue(self, guild_id: int):
        self.c.execute('DELETE FROM queue WHERE guild_id = ?', (guild_id,))
        self.conn.commit()

    def clear_all_queues(self):
        self.c.execute('DELETE FROM queue')
        self.conn.commit()

    def get_music_channel(self, guild_id: int) -> int:
        self.c.execute('SELECT music_channel_id FROM guild_settings WHERE guild_id = ?', (guild_id,))
        result = self.c.fetchone()
        return result[0] if result else None

    def set_music_channel(self, guild_id: int, channel_id: int):
        self.c.execute('''
            INSERT OR REPLACE INTO guild_settings (guild_id, music_channel_id)
            VALUES (?, ?)
        ''', (guild_id, channel_id))
        self.conn.commit()

    def save_queue(self, user_id: int, guild_id: int, queue_list: list, queue_name: str = None) -> dict:
        queue_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        try:
            queue_name = str(queue_name.value)
        except AttributeError:
            queue_name = str(queue_name) if queue_name else f"재생목록 #{queue_id}"

        self.c.execute('''
            INSERT INTO saved_queues (queue_id, user_id, guild_id, name, song_count)
            VALUES (?, ?, ?, ?, ?)
        ''', (queue_id, user_id, guild_id, queue_name, len(queue_list)))

        for position, song in enumerate(queue_list, 1):
            self.c.execute('''
                INSERT INTO saved_queue_songs 
                (queue_id, position, url, title, duration, channel, thumbnail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                queue_id,
                position,
                song['url'],
                song['title'],
                song.get('duration'),
                song.get('channel'),
                song.get('thumbnail')
            ))

        self.conn.commit()

        return {
            'queue_id': queue_id,
            'name': queue_name,
            'song_count': len(queue_list),
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

    def load_saved_queue(self, queue_id: str) -> list:
        self.c.execute('''
            SELECT url, title, duration, channel, thumbnail, position
            FROM saved_queue_songs
            WHERE queue_id = ?
            ORDER BY position ASC
        ''', (queue_id,))
        
        return [
            {
                'url': row[0],
                'title': row[1],
                'duration': row[2],
                'channel': row[3],
                'thumbnail': row[4],
                'position': row[5]
            }
            for row in self.c.fetchall()
        ]

    def get_queue_info(self, queue_id: str) -> dict:
        self.c.execute('''
            SELECT queue_id, user_id, name, created_at, song_count
            FROM saved_queues
            WHERE queue_id = ?
        ''', (queue_id,))
        
        row = self.c.fetchone()
        if row:
            return {
                'queue_id': row[0],
                'user_id': row[1],
                'name': row[2],
                'created_at': row[3],
                'song_count': row[4]
            }
        return None

    def shuffle_queue(self, guild_id: int):
        queue = self.get_queue(guild_id)
        if queue:
            shuffled_queue = queue.copy()
            random.shuffle(shuffled_queue)
            
            self.clear_guild_queue(guild_id)
            for i, song in enumerate(shuffled_queue, 1):
                song['position'] = i
                self.add_to_queue(guild_id, song)
            return True
        return False

    def sort_queue(self, guild_id: int):
        queue = self.get_queue(guild_id)
        if queue:
            sorted_queue = sorted(queue, key=lambda x: x['position'])
            
            self.clear_guild_queue(guild_id)
            for i, song in enumerate(sorted_queue, 1):
                song['position'] = i
                self.add_to_queue(guild_id, song)
            return True
        return False

    def close(self):
        self.conn.close()

    def save_music_player(self, guild_id: int, channel_id: int, message_id: int):
        self.c.execute('''
            INSERT OR REPLACE INTO music_players (guild_id, channel_id, message_id)
            VALUES (?, ?, ?)
        ''', (guild_id, channel_id, message_id))
        self.conn.commit()

    def get_music_players(self) -> list:
        self.c.execute('SELECT guild_id, channel_id, message_id FROM music_players')
        return self.c.fetchall()

    def remove_music_player(self, guild_id: int):
        self.c.execute('DELETE FROM music_players WHERE guild_id = ?', (guild_id,))
        self.conn.commit()

    def get_guild_settings(self, guild_id: int) -> GuildSettings:
        self.c.execute('''
            SELECT * FROM guild_settings WHERE guild_id = ?
        ''', (guild_id,))
        row = self.c.fetchone()
        if row:
            return GuildSettings.from_db(dict(row))
        return GuildSettings(guild_id)

db = QueueDB()

class SaveQueueModal(Modal):
    def __init__(self, queue_list):
        super().__init__(title='재생목록 저장')
        self.queue_list = queue_list
        
        self.queue_name = TextInput(
            label='재생목록 이름 (선택사항)',
            placeholder='미루에게 저장하고 싶은 재생목록의 이름을 알려줘!',
            required=False,
            max_length=50
        )
        self.add_item(self.queue_name)

    async def callback(self, interaction: nextcord.Interaction):
        if not self.queue_list:
            await interaction.response.send_message("❌ 음... 저장할 곡이 없는 것 같아...", ephemeral=True)
            return
            
        queue_info = db.save_queue(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            queue_list=self.queue_list,
            queue_name=self.queue_name.value if self.queue_name.value else None
        )

        try:
            dm_embed = nextcord.Embed(
                title="🎵 재생목록 저장 완료",
                description=f"재생목록을 미루에게 성공적으로 저장했어! 🤍",
                color=nextcord.Color.green()
            )
            dm_embed.add_field(
                name="재생목록 정보",
                value=f"```\n"
                      f"ID: {queue_info['queue_id']}\n"
                      f"이름: {queue_info['name']}\n"
                      f"곡 수: {queue_info['song_count']}곡\n"
                      f"저장 날짜: {queue_info['created_at']}\n"
                      f"```",
                inline=False
            )
            dm_embed.add_field(
                name="사용 방법",
                value="이 재생목록 불러오려면 검색창에 재생목록 ID를 입력해봐!\n"
                      "다른 서버에서도 이 ID를 쓸 수 있어! 😊",
                inline=False
            )
            await interaction.user.send(embed=dm_embed)
            
            await interaction.response.send_message(
                "✅ 재생목록을 미루에게 저장했어! DM을 확인해봐!",
                ephemeral=True
            )
        except nextcord.Forbidden:
            save_embed = nextcord.Embed(
                title="🎵 재생목록 저장 완료",
                description="DM을 보낼 수 없어서 여기에 정보를 표시할게..!",
                color=nextcord.Color.yellow()
            )
            save_embed.add_field(
                name="재생목록 정보",
                value=f"```\n"
                      f"ID: {queue_info['queue_id']}\n"
                      f"이름: {queue_info['name']}\n"
                      f"곡 수: {queue_info['song_count']}곡\n"
                      f"```",
                inline=False
            )
            await interaction.response.send_message(embed=save_embed, ephemeral=True)

class PlayingView(View):
    def __init__(self, message, song_info=None):
        super().__init__(timeout=None)
        self.message = message
        self.song_info = song_info

    async def interaction_check(self, interaction: nextcord.Interaction) -> bool:
        # 봇이 음성 채널에 없는 경우는 허용
        if not interaction.guild.voice_client:
            if not interaction.user.voice:
                await interaction.response.send_message(
                    "❌ 음성 채널에 먼저 들어가줘..!", 
                    ephemeral=True
                )
                return False
            return True
            
        # 봇이 음성 채널에 있는 경우
        if not interaction.user.voice:
            await interaction.response.send_message(
                "❌ 음성 채널에 먼저 들어와줘..!", 
                ephemeral=True
            )
            return False
        
        if interaction.guild.voice_client.channel != interaction.user.voice.channel:
            await interaction.response.send_message(
                f"❌ 여기는 미루가 있는 음성 채널이 아니야...\n{interaction.guild.voice_client.channel.mention}에 들어와줘!", 
                ephemeral=True
            )
            return False
            
        return True

    @nextcord.ui.button(label="노래 검색", style=nextcord.ButtonStyle.primary, row=0)
    async def search_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        search_lock = get_search_lock(interaction.guild_id)
        if search_lock.is_locked:
            if search_lock.current_user != interaction.user:
                await interaction.response.send_message(
                    f"❌ {search_lock.current_user.name}의 검색이 진행 중이야..! 잠시만 기다려줘!", 
                    ephemeral=True
                )
                return
        
        search_lock.is_locked = True
        search_lock.current_user = interaction.user
        
        modal = SearchModal(interaction.message, self)
        await interaction.response.send_modal(modal)

    @nextcord.ui.button(label="⏭️ 스킵", style=nextcord.ButtonStyle.secondary, row=0)
    async def skip_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        voice_client = interaction.guild.voice_client
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            queue = db.get_queue(interaction.guild_id)
            if queue:
                await interaction.response.send_message("⏭️ 다음 곡으로 넘어갈게!", ephemeral=True)
            else:
                await interaction.response.send_message("⏭️ 이게 마지막 곡이야!", ephemeral=True)
            voice_client.stop()
        else:
            await interaction.response.send_message("❌ 현재 재생 중인 노래가 없어..!", ephemeral=True)

    @nextcord.ui.button(label="재생목록 보기", style=nextcord.ButtonStyle.secondary, row=0)
    async def queue_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        queue = db.get_queue(interaction.guild_id)
        if not queue:
            await interaction.response.send_message("재생목록에 아무것도 없는 것 같은데?", ephemeral=True)
            return

        queue_embed = nextcord.Embed(
            title="🎵 재생목록",
            description=f"페이지 1/{((len(queue) - 1) // 5) + 1}",
            color=nextcord.Color.blue()
       )

        for song in queue[:5]:
            queue_embed.add_field(
                name=f"{song['position']}. {song['title']}",
                value=f"길이: {song['duration']} | 채널: {song['channel']}",
                inline=False
            )

        queue_view = QueueView(queue)
        queue_view.update_buttons()

        await interaction.response.send_message(
            embed=queue_embed,
            view=queue_view,
            ephemeral=True
        )

    @nextcord.ui.button(label="🔁 반복", style=nextcord.ButtonStyle.secondary, row=1)
    async def repeat_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        guild_id = interaction.guild_id
        current_state = get_repeat_state(guild_id)
        repeat_states[guild_id] = not current_state
        
        button.style = nextcord.ButtonStyle.success if repeat_states[guild_id] else nextcord.ButtonStyle.secondary
        
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"🔁 반복 재생을 {'켰어!' if repeat_states[guild_id] else '껐어!'}", 
            ephemeral=True
        )

    @nextcord.ui.button(label="🔀 셔플", style=nextcord.ButtonStyle.secondary, row=1)
    async def shuffle_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        guild_id = interaction.guild_id
        queue = db.get_queue(guild_id)
        
        if not queue:
            await interaction.response.send_message("❌ 재생목록이 비어있어..!", ephemeral=True)
            return
        
        current_state = get_shuffle_state(guild_id)
        shuffle_states[guild_id] = not current_state
        button.style = nextcord.ButtonStyle.success if shuffle_states[guild_id] else nextcord.ButtonStyle.secondary
        
        if shuffle_states[guild_id]:
            db.shuffle_queue(guild_id)
            await interaction.response.send_message("🔀 재생목록을 마구마구 섞어버렸어!", ephemeral=True)
        else:
            db.sort_queue(guild_id)
            await interaction.response.send_message("🔀 셔플을 해제했어!", ephemeral=True)
        
        await interaction.message.edit(view=self)

    @nextcord.ui.button(label="나가기", style=nextcord.ButtonStyle.danger, row=1)
    async def leave_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        voice_client = interaction.guild.voice_client
        if voice_client:
            # 먼저 응답 defer
            await interaction.response.defer(ephemeral=True)
            
            await voice_client.disconnect()
            current_playing.pop(interaction.guild_id, None)
            db.clear_guild_queue(interaction.guild_id)
            repeat_states[interaction.guild_id] = False
            shuffle_states[interaction.guild_id] = False
            
            initial_embed = nextcord.Embed(
                title="🎵 노래 부르는 미루",
                description="아래 버튼을 눌러서 미루에게 음악을 검색해봐!",
                color=nextcord.Color.blue()
            )
            await interaction.message.edit(
                embed=initial_embed,
                view=InitialView(interaction.message)
            )
            # followup 사용
            await interaction.followup.send("👋 미루 음성 채널에서 나갔어...", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 미루 이미 음성 채널에 없어.", ephemeral=True)

class QueueView(View):
    def __init__(self, queue_list, current_page=0):
        super().__init__(timeout=60)
        self.queue_list = queue_list
        self.current_page = current_page
        self.items_per_page = 5
        self.max_pages = ((len(queue_list) - 1) // self.items_per_page) + 1

    async def interaction_check(self, interaction: nextcord.Interaction) -> bool:
        if not interaction.guild.voice_client:
            if not interaction.user.voice:
                await interaction.response.send_message(
                    "❌ 음성 채널에 먼저 들어가줘..!", ephemeral=True)
                return False
            return True

        if not interaction.user.voice:
            await interaction.response.send_message(
                "❌ 음성 채널에 먼저 들어와줘..!", ephemeral=True)
            return False

        if interaction.guild.voice_client.channel != interaction.user.voice.channel:
            await interaction.response.send_message(
                f"❌ 여기는 미루가 있는 음성 채널이 아니야...\n{interaction.guild.voice_client.channel.mention}에 들어와줘!",
                ephemeral=True)
            return False

        return True

    @nextcord.ui.button(label="◀", style=nextcord.ButtonStyle.secondary, disabled=True, custom_id="prev")
    async def prev_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        self.current_page -= 1
        await self.update_page(interaction)

    @nextcord.ui.button(label="▶", style=nextcord.ButtonStyle.secondary, custom_id="next")
    async def next_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        self.current_page += 1
        await self.update_page(interaction)

    @nextcord.ui.button(label="재생목록 저장", style=nextcord.ButtonStyle.success, custom_id="save")
    async def save_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        current_song = get_current_playing_song(interaction.guild_id)
        full_queue = [current_song] if current_song else []
        full_queue.extend(self.queue_list)

        modal = SaveQueueModal(full_queue)
        await interaction.response.send_modal(modal)

    def update_buttons(self):
        for child in self.children:
            if isinstance(child, nextcord.ui.Button):
                if child.custom_id == "prev":
                    child.disabled = (self.current_page == 0)
                elif child.custom_id == "next":
                    child.disabled = (self.current_page >= self.max_pages - 1)

    async def update_page(self, interaction: nextcord.Interaction):
        self.update_buttons()

        start_idx = self.current_page * self.items_per_page
        end_idx = start_idx + self.items_per_page
        current_items = self.queue_list[start_idx:end_idx]

        queue_embed = nextcord.Embed(
            title="🎵 재생목록",
            description=f"페이지 {self.current_page + 1}/{self.max_pages}",
            color=nextcord.Color.blue()
        )

        for song in current_items:
            queue_embed.add_field(
                name=f"{song['position']}. {song['title']}",
                value=f"길이: {song['duration']} | 채널: {song['channel']}",
                inline=False
            )

        await interaction.response.edit_message(embed=queue_embed, view=self)


class SearchModal(Modal):
    def __init__(self, original_message, view):
        super().__init__(title="노래 검색")
        self.original_message = original_message
        self.view = view
        
        self.query = TextInput(
            label="검색어를 입력해줘!", 
            placeholder="노래 제목, YouTube 링크 또는 저장된 재생목록 ID를 입력해줘!\n취소하려면 'cancel' 또는 '취소'를 입력해!",
            min_length=1,
            max_length=100,
            required=True
        )
        self.add_item(self.query)

    async def callback(self, interaction: nextcord.Interaction):
        await interaction.response.defer()
        query = str(self.query.value)
        
        # 취소 명령어 체크
        if query.lower() in ['cancel', '취소']:
            current_song = get_current_playing_song(interaction.guild_id)
            if current_song and interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
                playing_embed = create_playing_embed(current_song)
                await self.original_message.edit(embed=playing_embed, view=PlayingView(self.original_message, current_song))
            else:
                initial_embed = nextcord.Embed(
                    title="🎵 노래 부르는 미루",
                    description="아래 버튼을 눌러서 미루에게 음악을 검색해봐!",
                    color=nextcord.Color.blue()
                )
                await self.original_message.edit(embed=initial_embed, view=InitialView(self.original_message))
            
            search_lock = get_search_lock(interaction.guild_id)
            search_lock.is_locked = False
            search_lock.current_user = None
            
            await interaction.followup.send("❌ 검색을 취소했어...", ephemeral=True)
            return

        try:
            # 재생목록 ID 체크 (6자리 영문/숫자)
            if re.match(r'^[A-Z0-9]{6}$', query):
                saved_queue = db.load_saved_queue(query)
                if not saved_queue:
                    await self.original_message.edit(
                        embed=nextcord.Embed(title="❌ 오류", description="엥..? 이건 미루가 모르는 재생목록 ID인데..?", color=nextcord.Color.red())
                    )
                    return

                queue_info = db.get_queue_info(query)
                loading_embed = nextcord.Embed(
                    title="📋 저장된 재생목록을 불러오는 중...",
                    description=f"'{queue_info['name']}' 재생목록을 불러오고 있어!",
                    color=nextcord.Color.blue()
                )
                loading_embed.add_field(
                    name="재생목록 정보",
                    value=f"총 {queue_info['song_count']}곡\n"
                          f"생성자: {await bot.fetch_user(queue_info['user_id'])}\n"
                          f"생성일: {queue_info['created_at']}"
                )
                await self.original_message.edit(embed=loading_embed)

                voice_client = interaction.guild.voice_client
                if not voice_client:
                    voice_client = await interaction.user.voice.channel.connect()

                if voice_client.is_playing():
                    for song in saved_queue:
                        db.add_to_queue(interaction.guild_id, song)
                    
                    success_embed = nextcord.Embed(
                        title="📋 저장된 재생목록 추가",
                        description=f"총 {len(saved_queue)}곡을 재생목록에 추가했어!",
                        color=nextcord.Color.green()
                    )
                    view = PlayingView(self.original_message, get_current_playing_song(interaction.guild_id))
                    await self.original_message.edit(embed=success_embed, view=view)
                    
                    # 3초 후 현재 재생 중인 노래 정보로 업데이트
                    await asyncio.sleep(3)
                    current_song = get_current_playing_song(interaction.guild_id)
                    if current_song:
                        playing_embed = create_playing_embed(current_song)
                        await self.original_message.edit(embed=playing_embed, view=view)
                else:
                    first_song = saved_queue[0]
                    remaining_songs = saved_queue[1:]

                    source = await get_audio_source(first_song['url'], interaction.guild_id)
                    
                    def after_playing(error):
                        asyncio.run_coroutine_threadsafe(
                            play_next(interaction.guild_id, self.original_message),
                            bot.loop
                        )

                    voice_client.play(source, after=after_playing)
                    set_current_playing_song(interaction.guild_id, first_song)

                    for song in remaining_songs:
                        db.add_to_queue(interaction.guild_id, song)

                    playing_embed = create_playing_embed(first_song)
                    playing_embed.description = f"저장된 재생목록의 나머지 {len(remaining_songs)}곡을 재생목록에 추가했어!"
                    await self.original_message.edit(embed=playing_embed, view=PlayingView(self.original_message))
                return

            # YouTube 링크 체크
            if "youtube.com/" in query or "youtu.be/" in query:
                # 플레이리스트 체크
                if "playlist" in query or "list=" in query:
                    loading_embed = nextcord.Embed(
                        title="📋 플레이리스트 감지",
                        description="플레이리스트를 불러오는 중...",
                        color=nextcord.Color.blue()
                    )
                    await self.original_message.edit(embed=loading_embed)

                    loop = asyncio.get_event_loop()
                    playlist_data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))

                    if not playlist_data:
                        raise Exception("플레이리스트를 불러올 수 없어...")

                    total_tracks = len(playlist_data['entries'])
                    loaded_tracks = 0

                    loading_embed = nextcord.Embed(
                        title="📋 플레이리스트 발견!",
                        description=f"{total_tracks}곡을 발견했어!\n불러오는 중... (0/{total_tracks})",
                        color=nextcord.Color.blue()
                    )
                    loading_embed.add_field(name="진행률", value="0%", inline=True)
                    await self.original_message.edit(embed=loading_embed)

                    first_track = None
                    playlist_tracks = []

                    for entry in playlist_data['entries']:
                        if entry:
                            loaded_tracks += 1
                            song_info = {
                                'url': entry['url'],
                                'title': entry['title'],
                                'duration': entry.get('duration_string', 'N/A'),
                                'channel': entry.get('uploader', 'N/A'),
                                'thumbnail': entry.get('thumbnail')
                            }

                            if not first_track:
                                first_track = song_info
                            else:
                                playlist_tracks.append(song_info)

                            progress = (loaded_tracks / total_tracks) * 100
                            loading_embed.description = f"{total_tracks}곡을 발견했어!\n불러오는 중... ({loaded_tracks}/{total_tracks})"
                            loading_embed.set_field_at(0, name="진행률", value=f"{progress:.1f}%", inline=True)
                            
                            if loaded_tracks % 5 == 0:
                                try:
                                    await self.original_message.edit(embed=loading_embed)
                                except nextcord.errors.HTTPException:
                                    pass

                    voice_client = interaction.guild.voice_client
                    if not voice_client:
                        voice_client = await interaction.user.voice.channel.connect()

                    if voice_client.is_playing():
                        all_tracks = [first_track] + playlist_tracks
                        for track in all_tracks:
                            db.add_to_queue(interaction.guild_id, track)
                        
                        success_embed = nextcord.Embed(
                            title="📋 플레이리스트 재생목록에 추가",
                            description=f"총 {total_tracks}곡을 재생목록에 추가했어!",
                            color=nextcord.Color.green()
                        )
                        view = PlayingView(self.original_message, get_current_playing_song(interaction.guild_id))
                        await self.original_message.edit(embed=success_embed, view=view)
                        
                        # 3초 후 현재 재생 중인 노래 정보로 업데이트
                        await asyncio.sleep(3)
                        current_song = get_current_playing_song(interaction.guild_id)
                        if current_song:
                            playing_embed = create_playing_embed(current_song)
                            await self.original_message.edit(embed=playing_embed, view=view)
                    else:
                        source = await get_audio_source(first_track['url'], interaction.guild_id)
                        
                        def after_playing(error):
                            asyncio.run_coroutine_threadsafe(
                                play_next(interaction.guild_id, self.original_message),
                                bot.loop
                            )

                        voice_client.play(source, after=after_playing)
                        set_current_playing_song(interaction.guild_id, first_track)

                        for track in playlist_tracks:
                            db.add_to_queue(interaction.guild_id, track)

                        playing_embed = create_playing_embed(first_track)
                        playing_embed.description = f"플레이리스트의 나머지 {len(playlist_tracks)}곡을 재생목록에 추가했어!"
                        await self.original_message.edit(embed=playing_embed, view=PlayingView(self.original_message))

                else:  # 단일 영상 링크
                    loading_embed = nextcord.Embed(
                        title="🎵 영상 불러오는 중...",
                        color=nextcord.Color.blue()
                    )
                    await self.original_message.edit(embed=loading_embed)

                    song_info = await get_song_info(query, interaction.guild_id)

                    voice_client = interaction.guild.voice_client
                    if not voice_client:
                        voice_client = await interaction.user.voice.channel.connect()

                    if voice_client.is_playing():
                        position = db.add_to_queue(interaction.guild_id, song_info)
                        
                        queue_embed = nextcord.Embed(
                            title="🎵 재생목록에 추가",
                            color=nextcord.Color.blue()
                        )
                        queue_embed.add_field(name="제목", value=song_info['title'], inline=False)
                        queue_embed.add_field(name="재생목록 위치", value=f"{position}번째", inline=True)
                        if song_info['thumbnail']:
                            queue_embed.set_thumbnail(url=song_info['thumbnail'])
                        
                        view = PlayingView(self.original_message, get_current_playing_song(interaction.guild_id))
                        await self.original_message.edit(embed=queue_embed, view=view)
                        
                        # 3초 후 현재 재생 중인 노래 정보로 업데이트
                        await asyncio.sleep(3)
                        current_song = get_current_playing_song(interaction.guild_id)
                        if current_song:
                            playing_embed = create_playing_embed(current_song)
                            await self.original_message.edit(embed=playing_embed, view=view)
                    else:
                        source = await get_audio_source(song_info['url'], interaction.guild_id)
                        
                        def after_playing(error):
                            asyncio.run_coroutine_threadsafe(
                                play_next(interaction.guild_id, self.original_message),
                                bot.loop
                            )

                        voice_client.play(source, after=after_playing)
                        set_current_playing_song(interaction.guild_id, song_info)

                        playing_embed = create_playing_embed(song_info)
                        await self.original_message.edit(embed=playing_embed, view=PlayingView(self.original_message))

            else:  # 일반 검색어
                results = YoutubeSearch(query, max_results=5).to_dict()
                if not results:
                    await self.original_message.edit(
                        embed=nextcord.Embed(title="❌ 검색 실패", description="미루... 못 찾겠어... 🥺", color=nextcord.Color.red())
                    )
                    return

                results_embed = nextcord.Embed(
                    title="🎵 검색 결과",
                    description="아래 버튼을 눌러서 곡을 선택해줘!",
                    color=nextcord.Color.blue()
                )
                for i, result in enumerate(results, 1):
                    results_embed.add_field(
                        name=f"{i}. {result['title']}",
                        value=f"⏱ {result['duration']} | 👤 {result['channel']}",
                        inline=False
                    )

                select_view = SongSelectView(results, interaction, self.original_message)
                await self.original_message.edit(embed=results_embed, view=select_view)

        except Exception as e:
            error_embed = nextcord.Embed(
                title="❌ 오류 발생",
                description=str(e),
                color=nextcord.Color.red()
            )
            await self.original_message.edit(embed=error_embed)
        finally:
            search_lock = get_search_lock(interaction.guild_id)
            search_lock.is_locked = False
            search_lock.current_user = None

class SongSelectView(View):
    def __init__(self, results, original_interaction, message):
        super().__init__(timeout=60)
        self.results = results
        self.original_interaction = original_interaction
        self.message = message

        # 숫자 버튼 추가
        for i in range(len(results)):
            button = Button(
                style=nextcord.ButtonStyle.primary,
                label=str(i + 1),
                custom_id=str(i)
            )
            self.add_item(button)

        # 취소 버튼 추가
        cancel_button = Button(
            style=nextcord.ButtonStyle.danger,
            label="취소",
            custom_id="cancel"
        )
        self.add_item(cancel_button)
        
        # 버튼 콜백 설정
        for i, child in enumerate(self.children[:-1]):  # 마지막 버튼(취소)은 제외
            child.callback = self.create_button_callback(i)
        self.children[-1].callback = self.cancel_callback  # 취소 버튼

    async def cancel_callback(self, interaction: nextcord.Interaction):
        if interaction.user != self.original_interaction.user:
            await interaction.response.send_message(
                "❌ 검색한 유저만 취소할 수 있어..!", 
                ephemeral=True
            )
            return

        # 현재 재생중인 노래가 있다면 그 정보를 표시
        current_song = get_current_playing_song(interaction.guild_id)
        if current_song and interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            playing_embed = create_playing_embed(current_song)
            await interaction.message.edit(embed=playing_embed, view=PlayingView(interaction.message, current_song))
        else:
            initial_embed = nextcord.Embed(
                title="🎵 노래 부르는 미루",
                description="아래 버튼을 눌러서 미루에게 음악을 검색해봐!",
                color=nextcord.Color.blue()
            )
            await interaction.message.edit(embed=initial_embed, view=InitialView(interaction.message))

        # 검색 잠금 해제
        search_lock = get_search_lock(interaction.guild_id)
        search_lock.is_locked = False
        search_lock.current_user = None

        await interaction.response.send_message("❌ 검색을 취소했어...", ephemeral=True)

    async def interaction_check(self, interaction: nextcord.Interaction) -> bool:
        if interaction.user != self.original_interaction.user:
            await interaction.response.send_message(
                "❌ 검색한 유저만 선택할 수 있어..!", 
                ephemeral=True
            )
            return False

        if not interaction.user.voice:
            await interaction.response.send_message(
                "❌ 음성 채널에 먼저 들어가줘..!", 
                ephemeral=True
            )
            return False
        
        if (interaction.guild.voice_client and 
            interaction.guild.voice_client.channel != interaction.user.voice.channel):
            await interaction.response.send_message(
                f"❌ 여기는 미루가 있는 음성 채널이 아니야...\n{interaction.guild.voice_client.channel.mention}에 들어와줘!",
                ephemeral=True
            )
            return False
            
        return True

    def create_button_callback(self, index):
        async def button_callback(interaction: nextcord.Interaction):
            try:
                # 먼저 응답 지연 처리
                await interaction.response.defer()

                selected_video = self.results[index]
                video_url = f"https://youtube.com{selected_video['url_suffix']}"

                try:
                    voice_client = interaction.guild.voice_client
                    if not voice_client:
                        voice_client = await interaction.user.voice.channel.connect()

                    loading_embed = nextcord.Embed(
                        title="🎵 재생 준비 중...",
                        description=selected_video['title'],
                        color=nextcord.Color.yellow()
                    )
                    await interaction.message.edit(embed=loading_embed, view=None)

                    song_info = await get_song_info(video_url, interaction.guild_id)

                    if voice_client.is_playing():
                        position = db.add_to_queue(interaction.guild_id, song_info)

                        queue_embed = nextcord.Embed(
                            title="🎵 재생목록에 추가",
                            color=nextcord.Color.blue()
                        )
                        queue_embed.add_field(name="제목", value=song_info['title'], inline=False)
                        queue_embed.add_field(name="재생목록 위치", value=f"{position}번째", inline=True)
                        if song_info['thumbnail']:
                            queue_embed.set_thumbnail(url=song_info['thumbnail'])

                        view = PlayingView(interaction.message, get_current_playing_song(interaction.guild_id))
                        await interaction.message.edit(embed=queue_embed, view=view)

                        await asyncio.sleep(3)
                        current_song = get_current_playing_song(interaction.guild_id)
                        if current_song:
                            playing_embed = create_playing_embed(current_song)
                            await interaction.message.edit(embed=playing_embed, view=view)
                    else:
                        source = await get_audio_source(song_info['url'], interaction.guild_id)

                        def after_playing(error):
                            asyncio.run_coroutine_threadsafe(
                                play_next(interaction.guild_id, interaction.message),
                                bot.loop
                            )

                        voice_client.play(source, after=after_playing)
                        set_current_playing_song(interaction.guild_id, song_info)

                        playing_embed = create_playing_embed(song_info)
                        view = PlayingView(interaction.message, song_info)
                        await interaction.message.edit(embed=playing_embed, view=view)

                    search_lock = get_search_lock(interaction.guild_id)
                    search_lock.is_locked = False
                    search_lock.current_user = None

                except Exception as e:
                    error_embed = nextcord.Embed(
                        title="❌ 오류 발생",
                        description=str(e),
                        color=nextcord.Color.red()
                    )
                    await interaction.message.edit(embed=error_embed, view=PlayingView(interaction.message))

                    search_lock = get_search_lock(interaction.guild_id)
                    search_lock.is_locked = False
                    search_lock.current_user = None

            except nextcord.NotFound:
                # 상호작용이 만료된 경우
                print("Interaction expired")
            except Exception as e:
                print(f"Button callback error: {e}")

        return button_callback

    
class InitialView(View):
    def __init__(self, message=None):
        super().__init__(timeout=None)
        self.message = message

    async def interaction_check(self, interaction: nextcord.Interaction) -> bool:
        # 봇이 음성 채널에 없는 경우는 허용
        if not interaction.guild.voice_client:
            if not interaction.user.voice:
                await interaction.response.send_message(
                    "❌ 음성 채널에 먼저 들어가줘!", 
                    ephemeral=True
                )
                return False
            return True
            
        # 봇이 음성 채널에 있는 경우
        if not interaction.user.voice:
            await interaction.response.send_message(
                "❌ 음성 채널에 먼저 들어와줘!", 
                ephemeral=True
            )
            return False
        
        if interaction.guild.voice_client.channel != interaction.user.voice.channel:
            await interaction.response.send_message(
                f"❌ 여기는 미루가 있는 음성 채널이 아니야...\n{interaction.guild.voice_client.channel.mention}에 들어와줘!",
                ephemeral=True
            )
            return False
            
        return True

    @nextcord.ui.button(label="노래 검색", style=nextcord.ButtonStyle.primary)
    async def search_button(self, button: nextcord.ui.Button, interaction: nextcord.Interaction):
        search_lock = get_search_lock(interaction.guild_id)
        if search_lock.is_locked:
            if search_lock.current_user != interaction.user:
                await interaction.response.send_message(
                    f"❌ {search_lock.current_user.name}의 검색이 진행 중이야..! 잠시만 기다려줘!", 
                    ephemeral=True
                )
                return
        
        search_lock.is_locked = True
        search_lock.current_user = interaction.user
        
        modal = SearchModal(interaction.message, self)
        await interaction.response.send_modal(modal)

def create_playing_embed(song_info):
    embed = nextcord.Embed(
        title="🎵 현재 재생 중",
        color=nextcord.Color.green()
    )

    embed.add_field(
        name="제목",
        value=song_info['title'],
        inline=False
    )

    embed.add_field(
        name="길이",
        value=song_info.get('duration', 'N/A'),
        inline=True
    )

    embed.add_field(
        name="채널",
        value=song_info.get('channel', 'N/A'),
        inline=True
    )
    
    if song_info.get('thumbnail'):
        embed.set_thumbnail(url=song_info['thumbnail'])
    
    return embed

async def play_next(guild_id, message):
    lock = await play_manager.get_lock(guild_id)
    async with lock:
        try:
            next_song = db.get_next_song(guild_id)
            current_song = get_current_playing_song(guild_id)
            
            if get_repeat_state(guild_id) and current_song:
                db.add_to_queue(guild_id, current_song)
                if get_shuffle_state(guild_id):
                    db.shuffle_queue(guild_id)

            if next_song:
                voice_client = message.guild.voice_client
                if not voice_client or not voice_client.is_connected():
                    return

                try:
                    source = await get_audio_source(next_song['url'], guild_id)
                    
                    def after_playing(error):
                        if error:
                            print(f"Error playing next song: {error}")
                        asyncio.run_coroutine_threadsafe(
                            play_next(guild_id, message),
                            bot.loop
                        )

                    voice_client.play(source, after=after_playing)
                    set_current_playing_song(guild_id, next_song)

                    playing_embed = create_playing_embed(next_song)
                    view = PlayingView(message, next_song)
                    await message.edit(embed=playing_embed, view=view)
                    
                    # 음성 채널 타이머 시작
                    voice_state = get_voice_state(guild_id)
                    await voice_state.start_timer(voice_client, message)

                except Exception as e:
                    print(f"Error in play_next: {e}")
                    await play_next(guild_id, message)  # 오류 발생 시 다음 곡 시도
            else:
                if get_repeat_state(guild_id) and current_song:
                    db.add_to_queue(guild_id, current_song)
                    await play_next(guild_id, message)
                    return
                    
                voice_client = message.guild.voice_client
                if voice_client:
                    await get_voice_state(guild_id).handle_disconnect(voice_client, message)
                
                current_playing.pop(guild_id, None)
                db.clear_guild_queue(guild_id)

        except Exception as e:
            print(f"Error in play_next: {e}")
            await handle_play_error(guild_id, message)

async def handle_play_error(guild_id: int, message):
    """재생 오류 처리 함수"""
    try:
        # 상태 초기화
        current_playing.pop(guild_id, None)
        repeat_states.pop(guild_id, None)
        shuffle_states.pop(guild_id, None)
        
        try:
            db.clear_guild_queue(guild_id)
        except Exception as e:
            print(f"Error clearing queue: {e}")
        
        # 음성 클라이언트 연결 해제
        try:
            voice_client = message.guild.voice_client
            if voice_client and voice_client.is_connected():
                await voice_client.disconnect()
        except Exception as e:
            print(f"Error disconnecting voice client: {e}")
            
        # 메시지 업데이트
        try:
            # 메시지가 여전히 존재하는지 확인
            try:
                await message.fetch()
            except nextcord.NotFound:
                return
                
            error_embed = nextcord.Embed(
                title="❌ 오류 발생",
                description="음악 재생 중 오류가 발생했어... 시스템을 초기화할게...",
                color=nextcord.Color.red()
            )
            await message.edit(embed=error_embed, view=InitialView(message))
        except nextcord.HTTPException as e:
            print(f"Error updating error message: {e}")
            
    except Exception as e:
        print(f"Critical error in handle_play_error: {e}")

class ErrorHandler:
    @staticmethod
    async def handle_voice_error(error, guild_id, message):
        error_msg = f"Voice error in guild {guild_id}: {str(error)}"
        print(error_msg)
        
        try:
            if isinstance(error, nextcord.ClientException):
                await handle_play_error(guild_id, message)
            elif isinstance(error, nextcord.opus.OpusNotLoaded):
                # Opus 라이브러리 재로딩 시도
                try:
                    nextcord.opus.load_opus('libopus.so.0')
                except:
                    pass
                await handle_play_error(guild_id, message)
        except Exception as e:
            print(f"Error handler failed: {e}")

@bot.slash_command(name="음악채널", description="음악 명령어를 사용할 수 있는 채널을 설정할 수 있어!")
async def set_music_channel(interaction: nextcord.Interaction, channel: nextcord.TextChannel = SlashOption(description="음악 명령어를 사용할 채널을 선택해줘!", required=True)):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 이 명령어는 관리자만 사용할 수 있어!", ephemeral=True)
        return

    db.set_music_channel(interaction.guild_id, channel.id)
    
    embed = nextcord.Embed(
        title="✅ 음악 채널 설정 완료",
        description=f"음악 명령어 채널을 {channel.mention}로 설정했어!",
        color=nextcord.Color.green()
    )
    embed.add_field(
        name="아래 명령어를 사용할 수 있어!",
        value="/음악설정",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

class ConfirmMusicSetupView(View):
    def __init__(self, interaction: nextcord.Interaction):
        super().__init__(timeout=30)
        self.interaction = interaction

    @nextcord.ui.button(label="이 버튼을 눌러서 미루 사용에 동의해주세요! 🤍", style=nextcord.ButtonStyle.primary)
    async def confirm_button(self, button: Button, interaction: nextcord.Interaction):
        # 채널 제한 체크
        allowed_channel = db.get_music_channel(interaction.guild_id)
        if allowed_channel and interaction.channel.id != allowed_channel:
            allowed_channel_obj = interaction.guild.get_channel(allowed_channel)
            if allowed_channel_obj:
                await interaction.response.send_message(
                    f"❌ 이 명령어는 {allowed_channel_obj.mention} 채널에서만 사용할 수 있어!",
                    ephemeral=True
                )
                return

        await interaction.response.defer()

        # 기존 메시지 제거
        async for message in interaction.channel.history(limit=100):
            if message.author == bot.user and message.embeds:
                embed = message.embeds[0]
                if embed.title in ["🎵 노래 부르는 미루", "🎵 현재 재생 중"]:
                    try:
                        db.remove_music_player(interaction.guild.id)
                        await message.delete()
                    except:
                        pass

        # 현재 재생 여부 확인
        current_song = get_current_playing_song(interaction.guild.id)

        if current_song and interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
            playing_embed = create_playing_embed(current_song)
            msg = await interaction.followup.send(embed=playing_embed, wait=True)
            await msg.edit(view=PlayingView(msg, current_song))
        else:
            initial_embed = nextcord.Embed(
                title="🎵 노래 부르는 미루",
                description="아래 버튼을 눌러서 미루에게 음악을 검색해봐!",
                color=nextcord.Color.blue()
            )
            msg = await interaction.followup.send(embed=initial_embed, wait=True)
            await msg.edit(view=InitialView(msg))

        db.save_music_player(interaction.guild.id, interaction.channel.id, msg.id)

# 슬래시 명령어 정의
@bot.slash_command(name="설정", description="미루 음악 설정을 시작할까요?")
async def ask_music_setup(interaction: nextcord.Interaction):
    view = ConfirmMusicSetupView(interaction)
    await interaction.response.send_message(
        "⚠️ 베타 테스트 안내\n현재 미루는 베타 테스트 단계에 있으며, 안정성과 기능이 완전히 보장되지 않을 수 있습니다.\n예기치 못한 오류나 비정상적인 동작이 발생할 수 있으며, 이는 지속적인 개선과 피드백을 통해 해결될 예정입니다.\n사용 중 문제가 발생할 경우, 너그러운 이해를 부탁드리며, 발견된 이슈는 공유해 주시면 큰 도움이 됩니다.\n미루는 더 나은 완성을 향해 발전하고 있습니다.",
        view=view,
        ephemeral=True
    )

@bot.event
async def on_ready():
    # 유저 인스톨톨
    guild = None

    context_types = [0, 1, 2]
    integration_types = [0, 1]

    commands = bot.get_all_application_commands()
    default_payload = [command.get_payload(guild_id=guild) for command in commands]

    for item in default_payload:
        item['contexts'] = context_types
        item['integration_types'] = integration_types

    data = await bot.http.bulk_upsert_global_commands(bot.application_id, payload=default_payload)

    print (data)

    # 봇 로그인
    print(f'Logged in as {bot.user}')
    
    # 캐시 클린업 태스크 시작
    bot.loop.create_task(cleanup_guild_caches())

    #상태표시
    await bot.change_presence(activity=nextcord.Activity(type=nextcord.ActivityType.listening, name="졸린 미루가 음악"), status=nextcord.Status.online)
    
    # 저장된 음악 플레이어 복구
    await restore_music_players()

async def restore_music_players():
    """저장된 음악 플레이어 메시지 복구"""
    try:
        players = db.get_music_players()
        restored_count = 0
        failed_count = 0
        
        for guild_id, channel_id, message_id in players:
            try:
                channel = bot.get_channel(channel_id)
                if not channel:
                    print(f"Channel {channel_id} not found for guild {guild_id}")
                    db.remove_music_player(guild_id)
                    failed_count += 1
                    continue

                try:
                    message = await channel.fetch_message(message_id)
                    if message:
                        # 메시지가 존재하면 현재 재생 중인 노래 확인
                        current_song = get_current_playing_song(guild_id)
                        if current_song and message.guild.voice_client and message.guild.voice_client.is_playing():
                            playing_embed = create_playing_embed(current_song)
                            await message.edit(embed=playing_embed, view=PlayingView(message, current_song))
                        else:
                            initial_embed = nextcord.Embed(
                                title="🎵 노래 부르는 미루",
                                description="아래 버튼을 눌러서 미루에게 음악을 검색해봐!",
                                color=nextcord.Color.blue()
                            )
                            await message.edit(embed=initial_embed, view=InitialView(message))
                        restored_count += 1
                except nextcord.NotFound:
                    db.remove_music_player(guild_id)
                    failed_count += 1
                except nextcord.Forbidden:
                    print(f"No permission to edit message in guild {guild_id}")
                    failed_count += 1
                except nextcord.HTTPException as e:
                    print(f"HTTP error restoring player in guild {guild_id}: {e}")
                    failed_count += 1
                    
            except Exception as e:
                print(f"Error restoring music player for guild {guild_id}: {e}")
                failed_count += 1
                continue

        print(f"Music players restored: {restored_count}, Failed: {failed_count}")
    except Exception as e:
        print(f"Critical error in restore_music_players: {e}")

@bot.event
async def on_error(event, *args, **kwargs):
    """전역 에러 핸들러"""
    error = args[0] if args else "Unknown"
    error_msg = f"Error in {event}: {error}"
    
    if isinstance(error, nextcord.errors.Forbidden):
        error_msg += " (Permission denied)"
    elif isinstance(error, nextcord.errors.NotFound):
        error_msg += " (Resource not found)"
    elif isinstance(error, nextcord.errors.HTTPException):
        error_msg += f" (HTTP {error.status}: {error.text})"
        
    print(error_msg)
    if kwargs:
        print(f"Additional info: {kwargs}")

@bot.event
async def on_voice_state_update(member, before, after):
    """음성 채널 상태 변경 이벤트 핸들러"""
    try:
        if member.bot:
            return
            
        guild_id = member.guild.id
        voice_client = member.guild.voice_client
        
        if not voice_client:
            return
            
        # 봇이 음성 채널에 혼자 남은 경우 체크
        if voice_client.channel and len([m for m in voice_client.channel.members if not m.bot]) == 0:
            voice_state = get_voice_state(guild_id)
            # 타이머 시작
            message = await get_player_message(member.guild)
            if message:
                await voice_state.start_timer(voice_client, message)
    except Exception as e:
        print(f"Error in voice state update: {e}")

async def get_player_message(guild):
    try:
        channel_id = db.get_music_channel(guild.id)
        if not channel_id:
            return None
            
        channel = guild.get_channel(channel_id)
        if not channel:
            return None
            
        players = db.get_music_players()
        for _, c_id, m_id in players:
            if c_id == channel_id:
                try:
                    return await channel.fetch_message(m_id)
                except:
                    continue
        return None
    except Exception as e:
        print(f"Error getting player message: {e}")
        return None

# GPT
@bot.event
async def on_message(message: nextcord.Message):
    if message.content.startswith("미루야"):
        question = message.content[len("미루야"):].strip()

        try:
            completion = openai.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "당신의 이름은 '미루'이고 나이는 '18'살 '여고생'입니다. 답변을 반말로 하고 답변에 애교를 최대한 많이 섞어주세요. 만약 당신을 누가 만들었는지 물어본다면 hyexn이라는 분이 만들었다고 대답해주세요. 만약 어떤 모델을 사용하는지 물어본다면 OpenAI의의 GPT-4.1 mini를 사용하고 있다고 대답해주세요. 언제 태어났냐고 물어본다면 2008년 05월 10일 이라고 대답하세요."
                    },
                    {"role": "user", "content": question}
                ],
                max_tokens=2000
            )

            response = completion.choices[0].message.content.strip()
            for chunk in [response[i:i + 2000] for i in range(0, len(response), 2000)]:
                await message.reply(chunk)

        except Exception as e:
            import traceback
            print("GPT 오류:", e)
            traceback.print_exc()
            await message.reply("앗! 이게 뭐야? 오류가 났나봐! 다시 한 번 해줄 수 있을까? 부탁해~ 💕")

    await bot.process_commands(message)

bot.run(os.getenv('DISCORD_TOKEN'))
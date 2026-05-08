import os
import json
import re
import subprocess
from datetime import datetime, timezone, timedelta
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from notion_client import Client
import pickle

JST = timezone(timedelta(hours=9))

def to_jst_date(utc_str):
    dt = datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
    return dt.astimezone(JST).strftime('%Y-%m-%d')

def parse_duration(duration_str):
    # ISO 8601 duration (PT1M30S) を秒に変換
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds

SCOPES = [
    'https://www.googleapis.com/auth/yt-analytics.readonly',
    'https://www.googleapis.com/auth/youtube.readonly'
]

CLIENT_SECRET = os.environ.get('CLIENT_SECRET_PATH', os.path.expanduser('~/ichigo-youtube/client_secret.json'))
TOKEN_FILE = os.environ.get('TOKEN_PATH', os.path.expanduser('~/ichigo-youtube/token.pickle'))
NOTION_TOKEN = os.environ.get('NOTION_TOKEN')
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS')
NOTION_DB_ID = '896f1c7810b249d88183ca6c74ce1dd0'

def get_youtube_credentials():
    if GOOGLE_CREDENTIALS:
        creds_data = json.loads(GOOGLE_CREDENTIALS)
        creds = Credentials(
            token=creds_data['token'],
            refresh_token=creds_data['refresh_token'],
            token_uri=creds_data['token_uri'],
            client_id=creds_data['client_id'],
            client_secret=creds_data['client_secret'],
            scopes=creds_data['scopes']
        )
        creds.refresh(Request())
        return creds
    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'rb') as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            import webbrowser
            webbrowser.open = lambda url, new=0, autoraise=True: subprocess.run(['open', url])
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            print('\nブラウザが開きます。Googleアカウントで許可してください。')
            creds = flow.run_local_server(port=8080, open_browser=True)
        with open(TOKEN_FILE, 'wb') as f:
            pickle.dump(creds, f)
    return creds

def get_video_list(youtube):
    # search.listで動画IDを取得
    video_ids = []
    request = youtube.search().list(
        part='id',
        forMine=True,
        type='video',
        maxResults=50
    )
    while request:
        response = request.execute()
        for item in response.get('items', []):
            video_ids.append(item['id']['videoId'])
        request = youtube.search().list_next(request, response)

    # videos.listで正確な公開日・尺・説明文を取得（公開動画のみ）
    videos = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i+50]
        res = youtube.videos().list(
            part='snippet,contentDetails,status',
            id=','.join(chunk)
        ).execute()
        for item in res.get('items', []):
            # 非公開・限定公開はスキップ
            if item['status']['privacyStatus'] != 'public':
                print(f"  スキップ（非公開）: {item['snippet']['title']}")
                continue
            duration_sec = parse_duration(item['contentDetails']['duration'])
            videos.append({
                'id': item['id'],
                'title': item['snippet']['title'],
                'published_at': to_jst_date(item['snippet']['publishedAt']),
                'description': item['snippet'].get('description', ''),
                'duration_sec': duration_sec,
            })
    return videos

def get_video_analytics(analytics, video_id):
    result = {}
    # 基本指標を取得
    try:
        response = analytics.reports().query(
            ids='channel==MINE',
            startDate='2020-01-01',
            endDate='2099-12-31',
            metrics='views,likes,comments,subscribersGained,averageViewDuration,averageViewPercentage',
            dimensions='video',
            filters=f'video=={video_id}'
        ).execute()
        rows = response.get('rows', [])
        if rows:
            r = rows[0]
            views = int(r[1])
            likes = int(r[2])
            comments = int(r[3])
            engagement = likes + comments
            result = {
                '再生数': views,
                '高評価': likes,
                'コメント数': comments,
                'エンゲージメント': engagement,
                'エンゲージメント率': engagement / views if views > 0 else 0,
                '登録者増加': int(r[4]),
                '平均視聴時間_秒': float(r[5]),
                '平均視聴率': float(r[6]) / 100,
            }
    except Exception as e:
        print(f'  Analytics error: {e}')

    return result

def get_existing_notion_videos(notion):
    existing = {}
    results = notion.databases.query(database_id=NOTION_DB_ID)
    for page in results['results']:
        props = page['properties']
        vid_id = props.get('YouTube動画ID', {}).get('rich_text', [])
        if vid_id:
            existing[vid_id[0]['text']['content']] = page['id']
    return existing

def update_notion(notion, page_id, video, analytics_data, thumbnail_url=None):
    props = {}
    if video.get('published_at'):
        props['公開日'] = {'date': {'start': video['published_at']}}
    if video.get('duration_sec'):
        props['動画尺_秒'] = {'number': video['duration_sec']}
    if video.get('description') is not None:
        props['説明文'] = {'rich_text': [{'text': {'content': video['description'][:2000]}}]}
    if '再生数' in analytics_data:
        props['再生数'] = {'number': analytics_data['再生数']}
    if '高評価' in analytics_data:
        props['高評価'] = {'number': analytics_data['高評価']}
    if 'コメント数' in analytics_data:
        props['コメント数'] = {'number': analytics_data['コメント数']}
    if 'エンゲージメント' in analytics_data:
        props['エンゲージメント'] = {'number': analytics_data['エンゲージメント']}
    if 'エンゲージメント率' in analytics_data:
        props['エンゲージメント率'] = {'number': analytics_data['エンゲージメント率']}
    if '登録者増加' in analytics_data:
        props['登録者増加'] = {'number': analytics_data['登録者増加']}
    if '平均視聴時間_秒' in analytics_data:
        props['平均視聴時間_秒'] = {'number': analytics_data['平均視聴時間_秒']}
    if '平均視聴率' in analytics_data:
        props['平均視聴率'] = {'number': analytics_data['平均視聴率']}
    if 'フィードからの流入率' in analytics_data:
        props['フィードからの流入率'] = {'number': analytics_data['フィードからの流入率']}
    update_params = {'page_id': page_id, 'properties': props}
    if thumbnail_url:
        update_params['cover'] = {'type': 'external', 'external': {'url': thumbnail_url}}
    if props or thumbnail_url:
        notion.pages.update(**update_params)

def create_notion_page(notion, video):
    thumbnail_url = f"https://img.youtube.com/vi/{video['id']}/hqdefault.jpg"
    props = {
        'タイトル': {'title': [{'text': {'content': video['title']}}]},
        'YouTube動画ID': {'rich_text': [{'text': {'content': video['id']}}]},
        'サムネイル': {'url': thumbnail_url},
        '公開日': {'date': {'start': video['published_at']}},
        '動画尺_秒': {'number': video['duration_sec']},
        '説明文': {'rich_text': [{'text': {'content': video['description'][:2000]}}]},
    }
    response = notion.pages.create(
        parent={'database_id': NOTION_DB_ID},
        cover={'type': 'external', 'external': {'url': thumbnail_url}},
        properties=props
    )
    print(f"  新規追加: {video['title']}")
    return response['id']

def main():
    auto_mode = bool(GOOGLE_CREDENTIALS)
    cutoff_days = 30

    print('YouTube認証中...')
    creds = get_youtube_credentials()
    youtube = build('youtube', 'v3', credentials=creds)
    analytics = build('youtubeAnalytics', 'v2', credentials=creds)
    notion = Client(auth=NOTION_TOKEN)

    print('動画一覧取得中...')
    videos = get_video_list(youtube)
    print(f'{len(videos)}本の動画を取得しました')

    if auto_mode:
        cutoff = (datetime.now(JST) - timedelta(days=cutoff_days)).strftime('%Y-%m-%d')
        videos = [v for v in videos if v['published_at'] >= cutoff]
        print(f'自動モード: 直近{cutoff_days}日の{len(videos)}本を更新します')

    print('Notion既存データ確認中...')
    existing = get_existing_notion_videos(notion)

    for video in videos:
        vid_id = video['id']
        print(f"処理中: {video['title']}")
        analytics_data = get_video_analytics(analytics, vid_id)
        thumbnail_url = f"https://img.youtube.com/vi/{vid_id}/hqdefault.jpg"

        if vid_id in existing:
            update_notion(notion, existing[vid_id], video, analytics_data, thumbnail_url)
            print(f"  更新完了")
        else:
            page_id = create_notion_page(notion, video)
            if analytics_data:
                update_notion(notion, page_id, video, analytics_data)

    print('完了！')

if __name__ == '__main__':
    main()

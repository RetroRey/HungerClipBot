# Hunger Twitch Clips → YouTube daily bot

Every day this grabs the most-viewed clips from Twitch's **Hunger** category from the last 24 hours, stitches them into one 1080p video with each streamer's name in a caption box, and uploads it to your YouTube channel. The description credits every streamer with a link and timestamp (which also gives the video YouTube chapters).

It runs for free on GitHub Actions — no server or computer left on.

## One-time setup (about 20 minutes)

### 1. Twitch API keys
1. Go to https://dev.twitch.tv/console/apps and click **Register Your Application**.
2. Name: anything. OAuth Redirect URL: `http://localhost`. Category: *Application Integration*. Client type: *Confidential*.
3. Open the app, copy the **Client ID**, click **New Secret** and copy the **Client Secret**.

### 2. YouTube API access
1. Go to https://console.cloud.google.com, create a project.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen** → External → fill in the name/email → add yourself under **Test users** → then click **Publish app** (otherwise your login expires every 7 days).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** → type **Desktop app** → download the JSON and rename it `client_secret.json`.
5. On your own computer, in this folder:
   ```
   pip install google-auth-oauthlib
   python get_youtube_token.py
   ```
   Sign in with the account that owns the channel. It prints three values.

### 3. Put it on GitHub
1. Create a new **private** repo on GitHub and upload everything in this folder (but **not** `client_secret.json`).
2. Repo **Settings → Secrets and variables → Actions → New repository secret**, add these five:
   - `TWITCH_CLIENT_ID`
   - `TWITCH_CLIENT_SECRET`
   - `YT_CLIENT_ID`
   - `YT_CLIENT_SECRET`
   - `YT_REFRESH_TOKEN`

### 4. Set your featured channel and test
1. Edit `config.yaml` — set `featured_channel` to the channel's login name (the part after `twitch.tv/`).
2. Set `privacy_status: "private"` for the first test.
3. **Actions** tab → *Daily Twitch clips upload* → **Run workflow**.
4. Check your channel. The finished video is also downloadable from the run page under *Artifacts*.
5. Happy with it? Set `privacy_status` back to `"public"`. It now posts every day at 22:00 UTC (change the `cron` line in `.github/workflows/daily.yml`).

## Daily rules
- **Hunger only.** Clips come only from the Hunger category.
- **Featured channel.** If your featured channel has Hunger clips from the last 24 hours, its 2 most-viewed ones always go in, even if they'd rank too low otherwise (and even if they aren't in English). If it has only 1, that one goes in; if none, the video is made without it. It never gets more than 2.
- **Minimum to post.** A video is only made if there are **at least 10 different clips OR at least 5 minutes of footage**. Otherwise the run logs `SKIPPED` and nothing is posted that day. This is checked again after downloading, so a failed download can't sneak a short video through.

## Settings (`config.yaml`)
| Setting | What it does |
|---|---|
| `featured_channel`, `featured_clips` | Channel that always gets its clips in, and how many |
| `min_clips`, `min_total_seconds` | Minimum to post (either one is enough) |
| `lookback_hours` | How far back to look for clips |
| `max_clips` / `max_total_seconds` | Video length limits |
| `max_clips_per_streamer` | Variety — stops one streamer dominating |
| `language` | Only clips in this language |
| `caption_position`, `caption_font_size` | Where/how big the name box is |
| `title_template`, `description_header`, `tags` | YouTube text |

## Running locally
```
pip install -r requirements.txt     # also needs ffmpeg installed
export TWITCH_CLIENT_ID=... TWITCH_CLIENT_SECRET=...
python bot.py --no-upload           # builds out/compilation.mp4 without posting
```

## Things to know
- **Uploads may be stuck on Private at first.** YouTube locks API uploads from new, unverified Google Cloud projects to private. To post publicly, submit the free **YouTube API Services audit** form (search "YouTube API audit and quota extension form"). Until approved, you can let the bot upload as private and flip each video to public by hand.
- **Permission & monetization.** These clips belong to the streamers. Crediting them helps, but it isn't permission — some streamers welcome compilations, others file copyright claims. YouTube also restricts monetization on "reused content," so a pure clip compilation may not be accepted into the Partner Program without added commentary or editing. Many successful clip channels ask streamers for permission or have a takedown-on-request policy.
- **GitHub pauses schedules** on repos with no commits for 60 days. If uploads stop, push any small change or click *Run workflow*.
- Non-Latin display names (e.g. Japanese) are captioned with the channel's login name instead, since the font can't draw them.

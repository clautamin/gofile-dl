# gofile-dl

A Python script for downloading stuff from [gofile.io](https://gofile.io) without a browser. Give it a link and it grabs everything in it, subfolders included.

I wrote it because clicking through big gofile folders one file at a time gets old fast. It downloads a few files at the same time, picks up where it left off if your connection drops, and works with password-protected links.

## Setup

You need Python 3.8 or newer.

```bash
git clone https://github.com/clautamin/gofile-dl.git
cd gofile-dl
pip install -r requirements.txt
```

The only dependency is `requests`.

## How to use it

Pass it a gofile link:

```bash
python gofile_downloader.py https://gofile.io/d/AbC123
```

It looks something like this while it runs (the last line keeps updating until everything's done):

```text
Fetching AbC123 ...
Found 2 file(s), 1.4 GB
done: /home/me/Downloads/AbC123/Extras/notes.txt
done: /home/me/Downloads/AbC123/video.mp4
[2/2] 1.4 GB / 1.4 GB (100.0%)  11.8 MB/s
All done.
```

If the link has a password, put it after the link:

```bash
python gofile_downloader.py https://gofile.io/d/AbC123 mypassword
```

You can also use only the id (`AbC123`) instead of the full link.

### Lots of links

Put them in a text file, one per line, and pass the file instead of a link. If a link needs a password, add it after a space. Lines starting with `#` are skipped. There's an example in [urls.example.txt](urls.example.txt).

```text
https://gofile.io/d/AbC123
https://gofile.io/d/XyZ789 secretpassword
```

```bash
python gofile_downloader.py urls.txt
```

### Only some of the files

If you don't want everything in a folder, set `GF_INTERACTIVE=1`. The script lists the files and asks which ones you want. Type something like `1,3-5`, or press Enter to get them all.

On Linux or macOS:

```bash
GF_INTERACTIVE=1 python gofile_downloader.py https://gofile.io/d/AbC123
```

On Windows (PowerShell):

```powershell
$env:GF_INTERACTIVE = "1"
python gofile_downloader.py https://gofile.io/d/AbC123
```

## Where the files end up

By default, downloads go into the folder you run the script from. Each link gets its own folder named after its id, and subfolders are kept the way they are on gofile:

```text
AbC123/
├── video.mp4
└── Extras/
    └── notes.txt
```

A link that points to a single file is saved directly, without the extra folder.

## If a download gets interrupted

Just run the same command again. While a file is downloading it's saved as `something.part`, and the script continues from there the next time. Files that are already finished are skipped, so re-running is safe.

## Settings

Everything is set with environment variables, and you don't have to set any of them.

| Variable | Default | What it does |
| --- | --- | --- |
| `GF_DOWNLOAD_DIR` | current folder | Where to save downloads |
| `GF_MAX_CONCURRENT_DOWNLOADS` | `5` | How many files to download at once |
| `GF_MAX_RETRIES` | `5` | How many times to retry a file when the connection fails |
| `GF_TIMEOUT` | `15` | Seconds to wait before giving up on a connection |
| `GF_USERAGENT` | `Mozilla/5.0` | User-Agent sent to gofile |
| `GF_CHUNK_SIZE` | `2097152` | Bytes read at a time (2 MB) |
| `GF_TOKEN` | none | Your gofile account token, if you have one |
| `GF_INTERACTIVE` | off | Set to `1` to choose files before downloading |

You don't need a gofile account. The script creates a temporary guest one every time it runs. If you do have an account, you can find your token in your gofile account settings and set it as `GF_TOKEN`.

## When something goes wrong

- **`error-notFound`**: the link was deleted, or the id is wrong.
- **`passwordWrong`**: wrong password. They're case-sensitive.
- **`rate limited by gofile`**: you're sending too many requests. Wait a few minutes, or set `GF_MAX_CONCURRENT_DOWNLOADS` to something lower like `2`.
- **`got an HTML page instead of the file`**: the download link expired. Running the command again usually fixes it.
- **Every link suddenly fails**: gofile probably changed something on their side. The script copies a small security check the gofile website does, and when they change that, the script needs an update. Feel free to open an issue.

When it's done, the script exits with `0` if everything downloaded and `2` if anything failed, in case you want to use it in your own scripts.

## Thanks

This started from [ltsdw/gofile-downloader](https://github.com/ltsdw/gofile-downloader), which figured out how gofile's API works. Go give it a star.

## Disclaimer

This isn't an official gofile tool, and I have nothing to do with gofile.io. Only download things you're allowed to, and stick to their [terms of service](https://gofile.io/terms).

## License

MIT. See [LICENSE](LICENSE).

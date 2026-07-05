# Automated Music Ingestion & Tagging

**System Host:** tower-two (10.50.0.25)
**Last Updated:** 2026-06-06

## Overview
This system provides hands-off metadata normalization and library archiving. It ensures that any file added to the library is correctly tagged for Swinsian, Radiologik DJ, and the Tower HUD.

## Directory Structure
- **Ingest Folder:** `/home/tower-two/wgxc-dashboard/music/ingest/`
  - Drop new files here using the naming convention: `Artist_Title_YYYY-MM-DD_EVERGREEN.mp3`
- **Archive Folder:** `/home/tower-two/wgxc-dashboard/music/primary/`
  - Permanent home for tagged files.
- **Log File:** `/home/tower-two/wgxc-dashboard/logs/ingest.log`

## Automated Components
1. **ingest-sentinel.sh** (Cron trigger)
   - Runs every 5 minutes.
   - Detects new files in `ingest/`, triggers the tagger, moves them to `primary/`, and rebuilds the HUD index.
2. **tagger.py** (Metadata Engine)
   - Uses Mutagen to write ID3v2.3 (MP3) and Vorbis (FLAC) tags.
   - Injects custom frames: `BROADCAST_DATE` and `IS_EVERGREEN`.
3. **build_index.py** (HUD Librarian)
   - Regenerates `~/artist_library.json` for the Winamp-style browser.

## Cron Configuration
```bash
*/5 * * * * /home/tower-two/ingest-sentinel.sh
```

## Naming Conventions
The tagger is flexible but prefers:
- `Artist_Title_Date_EVERGREEN.ext`
- Fallback: `01 Title.ext` (extracts Artist from existing tags)

## Future Migration (Synology)
To move this to Synology:
1. Copy these scripts to the NAS.
2. Update paths in `ingest-sentinel.sh`.
3. Set up Synology Task Scheduler to run the sentinel every 5 minutes.

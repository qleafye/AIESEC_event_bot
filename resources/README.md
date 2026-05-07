# resources/

This folder contains fallback images used when no photo has been uploaded through the admin panel.

The bot checks the database first — these files are only used as a last resort.

## Expected files

| File          | Used for                     |
|---------------|------------------------------|
| `start.jpg`   | Welcome message photo (/start) |
| `venue.jpg`   | Venue info photo              |
| `program.jpg`  | Event program photo           |

All files are optional. If a file is missing and no photo is set in the admin panel, the bot sends a text-only message instead.

Upload your own images through the bot's admin panel (`/admin` → Settings) — no need to replace these files manually.

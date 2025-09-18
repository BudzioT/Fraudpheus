# Slack to Mattermost Migration Instructions

## Overview
This script extracts ALL messages and threads from your Slack workspace for migration to Mattermost. It will:
- Fetch all threads and their complete message history
- Capture user information, files, reactions, and metadata
- Post live status updates every minute in the Fraudpheus channel (C096P2QHTM5)
- Generate a comprehensive JSON export file

## Prerequisites

1. **Environment Variables**: Ensure your `.env` file contains:
   ```
   SLACK_BOT_TOKEN=xoxb-your-bot-token
   CHANNEL_ID=C096P2QHTM5
   ```

2. **Python Dependencies**: Install required packages:
   ```bash
   pip install -r requirements.txt
   ```

## How to Run

### Simple Execution
```bash
python slack_to_mattermost_migration.py
```

### Background Execution (Recommended)
For long-running migrations, use nohup to run in background:
```bash
nohup python slack_to_mattermost_migration.py > migration.log 2>&1 &
```

### Monitor Progress
The script will post status updates every minute to the Fraudpheus channel. You can also monitor the log file:
```bash
tail -f migration.log
```

## What the Script Does

1. **Channel Scanning**: Scans the entire channel to identify all threads
2. **Data Extraction**: For each thread:
   - Extracts root message and all replies
   - Captures user information (name, email, avatar, etc.)
   - Downloads file metadata (files themselves need separate handling)
   - Preserves reactions and edit history
   - Records timestamps and thread structure

3. **Status Updates**: Posts live progress to channel:
   - Current thread count
   - Total messages processed
   - Runtime statistics
   - Estimated completion

4. **Export Generation**: Creates timestamped JSON file with complete data

## Output File Structure

The generated `slack_export_YYYYMMDD_HHMMSS.json` contains:

```json
{
  "export_timestamp": "ISO timestamp",
  "channel_id": "C096P2QHTM5",
  "channel_info": {
    "name": "fraudpheus",
    "purpose": "...",
    "num_members": 123
  },
  "threads": [
    {
      "thread_ts": "1234567890.123456",
      "root_message": {
        "ts": "...",
        "user": "U123456",
        "text": "...",
        "files": [...],
        "reactions": [...]
      },
      "replies": [...],
      "participants": ["U123456", "U789012"],
      "created_at": "ISO timestamp",
      "last_activity": "ISO timestamp"
    }
  ],
  "users": {
    "U123456": {
      "name": "username",
      "real_name": "Real Name",
      "email": "user@domain.com",
      "avatar": "https://...",
      "is_bot": false
    }
  },
  "statistics": {
    "total_threads": 1234,
    "total_messages": 5678,
    "total_users": 89,
    "processing_time_seconds": 3600
  }
}
```

## Performance Expectations

- **Small workspace** (< 1000 messages): 5-10 minutes
- **Medium workspace** (1000-10000 messages): 30-60 minutes
- **Large workspace** (> 10000 messages): 1-3 hours

The script includes API rate limiting (1 second between requests) to avoid hitting Slack's limits.

## Troubleshooting

### Permission Errors
Ensure your bot token has these scopes:
- `channels:history`
- `channels:read`
- `users:read`
- `files:read`
- `chat:write`

### Memory Issues
For very large workspaces, the script loads all data into memory. If you encounter memory issues:
1. Run on a machine with more RAM
2. Consider modifying the script to process in batches

### API Rate Limits
If you hit rate limits:
1. The script includes built-in delays
2. Increase the `time.sleep(1)` value in the main loop

## File Handling

**Important**: This script captures file metadata but does NOT download actual files. For complete migration:

1. Use Slack's built-in export feature for files
2. Or extend this script to download files using the `url_private` URLs
3. Files will need to be re-uploaded to Mattermost separately

## Next Steps

After running this script:

1. **Review the export**: Check the generated JSON file
2. **Import to Mattermost**: Use Mattermost's bulk import tools
3. **User mapping**: Map Slack user IDs to Mattermost usernames
4. **File migration**: Handle file transfers separately
5. **Testing**: Verify thread structure and message content

## Support

If you encounter issues:
1. Check the migration.log file for detailed errors
2. Verify your Slack bot permissions
3. Ensure environment variables are correctly set
4. Monitor the status updates in the Fraudpheus channel
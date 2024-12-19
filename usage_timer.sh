#!/bin/bash


# Configuration
USAGE_LIMIT=$((6 * 3600)) # 6 hours in seconds
WARNING_TIME=$((30 * 60)) # 30 minutes in seconds
LOG_FILE="/tmp/usage_timer.log"

# Commands
NOTIFY_CMD="notify-send"
SHUTDOWN_CMD="sudo poweroff"

# Initialize
START_TIME=$(date +%s)

# Logging Function
log() {
    echo "$(date): $1" >> "$LOG_FILE"
}

# Notification Function
send_notification() {
    $NOTIFY_CMD "$1" "$2" -u critical
    log "Notification sent: $1 - $2"
}

# Main Loop
log "Script started. Monitoring user time."

while true; do
    CURRENT_TIME=$(date +%s)
    ELAPSED_TIME=$((CURRENT_TIME - START_TIME))

    # Check for warning time
    if (( ELAPSED_TIME >= (USAGE_LIMIT - WARNING_TIME) && ELAPSED_TIME < USAGE_LIMIT )); then
        send_notification "Warning: Time Running Out!" "You have $(($USAGE_LIMIT - ELAPSED_TIME)) seconds left. Save your work!"
        WARNING_TIME=$((USAGE_LIMIT + 1)) # Prevent multiple notifications
    fi

    # Check for time limit
    if (( ELAPSED_TIME >= USAGE_LIMIT )); then
        send_notification "Time's Up!" "The system will shut down now."
        log "Shutting down the system."
        $SHUTDOWN_CMD
        break
    fi

    # Sleep to reduce CPU usage
    sleep 5
done



'#!/usr/bin/fish'
# Directory to watch for new .mp4 files
set WATCH_DIR /home/sam/Videos/Youtube/t/

# USB drive mount point
set USB_MOUNT_POINT /mnt/usb/

# Function to get file size from .file_sizes
function get_file_size
    set base_name $argv[1]
    set file_sizes_file $WATCH_DIR/.file_sizes
    if [ -f $file_sizes_file ]
        grep "^$base_name:" $file_sizes_file | cut -d':' -f2
    end
end

# Function to set file size in .file_sizes
function set_file_size
    set base_name $argv[1]
    set size $argv[2]
    set file_sizes_file $WATCH_DIR/.file_sizes
    if grep -q "^$base_name:" $file_sizes_file
        sed -i "s|^$base_name:.*|$base_name:$size|" $file_sizes_file
    else
        echo "$base_name:$size" >> $file_sizes_file
    end
end

# Infinite loop to watch for changes
while true
    for new_file in $WATCH_DIR/*.mp4
        set base_name (basename $new_file)
        if not string match -r '\.f[0-9]{3}\.mp4$' $base_name && not string match -r '\.temp\.mp4$' $base_name
            set old_size (get_file_size $base_name)
            set current_size (stat -c%s $new_file)
            if [ -z "$old_size" ] || [ $current_size -ne $old_size ]
                rsync -av --progress $new_file $USB_MOUNT_POINT/
                set_file_size $base_name $current_size
            end
        end
    end
    sleep 5
end

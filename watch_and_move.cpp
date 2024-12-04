#include <iostream>
#include <filesystem>
#include <unordered_map>
#include <regex>
#include <chrono>
#include <thread>

namespace fs = std::filesystem;

int main() {
    const std::string WATCH_DIR = "/home/sam/Videos/Youtube/t/";
    const std::string USB_MOUNT_POINT = "/mnt/usb/";

    // Regular expressions for file name filtering
    const std::regex regex_fxxx(R"(\\.f\\d{3}\\.mp4$)", std::regex_constants::icase);
    const std::regex regex_temp(R"(\\.temp\\.mp4$)", std::regex_constants::icase);

    // Map to store file sizes
    std::unordered_map<std::string, uintmax_t> file_sizes;

    // Check if watch directory exists
    if (!fs::exists(WATCH_DIR) || !fs::is_directory(WATCH_DIR)) {
        std::cerr << "Watch directory does not exist or is not a directory: " << WATCH_DIR << std::endl;
        return 1;
    }

    // Check if USB mount point exists and is writable
    if (!fs::exists(USB_MOUNT_POINT) || !fs::is_directory(USB_MOUNT_POINT) || !fs::is_writeable(USB_MOUNT_POINT)) {
        std::cerr << "USB mount point does not exist, is not a directory, or is not writable: " << USB_MOUNT_POINT << std::endl;
        return 1;
    }

    while (true) {
        for (const auto& entry : fs::directory_iterator(WATCH_DIR)) {
            if (entry.path().extension() == ".mp4") {
                std::string base_name = entry.path().filename().string();

                // Check if base_name does not match excluded patterns
                if (!std::regex_search(base_name, regex_fxxx) && !std::regex_search(base_name, regex_temp)) {
                    try {
                        uintmax_t current_size = fs::file_size(entry.path());
                        auto iter = file_sizes.find(base_name);
                        if (iter == file_sizes.end() || iter->second != current_size) {
                            // File is new or size has changed
                            std::string rsync_cmd = "rsync -av --progress '" + entry.path().string() + "' '" + USB_MOUNT_POINT + "'";
                            int ret = std::system(rsync_cmd.c_str());
                            if (ret != 0) {
                                std::cerr << "rsync failed for " << entry.path() << std::endl;
                            }
                            // Update file size in map
                            file_sizes[base_name] = current_size;
                        }
                    } catch (const fs::filesystem_error& e) {
                        std::cerr << "Filesystem error: " << e.what() << std::endl;
                    }
                }
            }
        }
        // Sleep for 5 seconds
        std::this_thread::sleep_for(std::chrono::seconds(5));
    }

    return 0;
}

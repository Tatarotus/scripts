#include <iostream>
#include <fstream>
#include <filesystem>
#include <string>

namespace fs = std::filesystem;

int main(int argc, char* argv[]) {
    if (argc != 2) {
        std::cerr << "Usage: " << argv[0] << " file_path" << std::endl;
        return 1;
    }

    std::string file_path = argv[1];

    size_t last_slash = file_path.find_last_of('/');
    std::string file_name;
    if (last_slash == std::string::npos) {
        file_name = file_path;
    } else {
        file_name = file_path.substr(last_slash + 1);
    }

    if (file_name.size() >= 4 && file_name.substr(file_name.size() - 4) == ".mp4") {
        if (fs::is_directory("/home/sam/Videos/Youtube")) {
            std::ifstream src(file_path, std::ios::binary);
            if (!src) {
                std::cerr << "Cannot open source file." << std::endl;
                return 1;
            }

            src.seekg(0, std::ios::end);
            std::streamoff file_size = src.tellg();
            src.seekg(0, std::ios::beg);

            std::string dest_path = "/home/sam/Videos/Youtube/" + file_name;
            std::ofstream dest(dest_path, std::ios::binary);
            if (!dest) {
                std::cerr << "Cannot open destination file for writing." << std::endl;
                return 1;
            }

            const int buffer_size = 1024 * 1024; // 1MB
            char buffer[buffer_size];
            std::streamoff total_read = 0;

            while (src.read(buffer, buffer_size)) {
                std::streamsize read_count = src.gcount();
                dest.write(buffer, read_count);
                if (!dest) {
                    std::cerr << "Error writing to destination file." << std::endl;
                    return 1;
                }
                total_read += read_count;
                double progress = static_cast<double>(total_read) / file_size * 100.0;
                std::cout << "Copied " << total_read << " of " << file_size << " bytes (" << progress << "%)" << std::endl;
            }

            if (!src) {
                std::cerr << "Error reading source file." << std::endl;
                return 1;
            }

            dest.flush();
            if (!dest) {
                std::cerr << "Error flushing destination file." << std::endl;
                return 1;
            }

            if (dest && src) {
                if (std::remove(file_path.c_str()) != 0) {
                    std::cerr << "Error deleting original file." << std::endl;
                    return 1;
                }
            } else {
                std::cerr << "File copy incomplete." << std::endl;
                return 1;
            }
        } else {
            std::cerr << "There is an error in the path" << std::endl;
            return 1;
        }
    }

    return 0;
}

#include "app/app.h"

#include <iostream>

int main() {
    std::cout << "Project Watashi local translation engine starting..." << std::endl;

    projectwatashi::App app;
    app.run();
    return 0;
}

#pragma once

#include <atomic>
#include <memory>

#include "ocr/screen_capturer.h"

namespace projectwatashi {

class RegionSelector {
public:
    RegionSelector();
    ~RegionSelector();

    bool startSelection();
    void stopSelection();
    ScreenRect getSelectedRect() const;
    bool isSelectionReady() const;

private:
    std::atomic_bool active_;
    ScreenRect selected_rect_;
    bool ready_ = false;
};

} // namespace projectwatashi

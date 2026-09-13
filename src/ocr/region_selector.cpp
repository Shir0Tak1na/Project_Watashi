#include "ocr/region_selector.h"

namespace projectwatashi {

RegionSelector::RegionSelector() = default;
RegionSelector::~RegionSelector() = default;

bool RegionSelector::startSelection() {
    active_ = true;
    ready_ = false;
    selected_rect_ = {0, 0, 0, 0};
    return true;
}

void RegionSelector::stopSelection() {
    active_ = false;
}

ScreenRect RegionSelector::getSelectedRect() const {
    return selected_rect_;
}

bool RegionSelector::isSelectionReady() const {
    return ready_;
}

} // namespace projectwatashi

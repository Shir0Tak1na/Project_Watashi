#include "watashi/ui/settings_window.hpp"

#include <algorithm>
#include <map>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

namespace watashi::ui {
namespace {

constexpr wchar_t kSettingsClass[] = L"WatashiSettingsWindow";
constexpr wchar_t kSettingsTitle[] = L"Watashi settings";

#ifndef EM_SETBKGNDCOLOR
// Edit-control message; not spelled out by every SDK header.
#define EM_SETBKGNDCOLOR (0x00F7)
#endif

constexpr int kSaveId = 100;
constexpr int kCancelId = 101;
constexpr int kResetId = 102;
constexpr int kReloadId = 103;

constexpr int kMargin = 18;
constexpr int kLabelWidth = 210;
constexpr int kControlX = 236;
constexpr int kControlWidth = 360;
constexpr int kBadgeX = 606;
constexpr int kBadgeWidth = 120;
constexpr int kDescriptionWidth = 730;
constexpr int kFooterHeight = 64;
constexpr int kClientWidth = 790;
// Short enough to sit above the bottom-centre overlay instead of under it.
constexpr int kClientHeight = 620;

bool class_registered = false;

std::wstring widen(const std::string& text) {
    if (text.empty()) {
        return std::wstring();
    }
    const int length = MultiByteToWideChar(
        CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
    std::wstring wide(static_cast<std::size_t>(length), L'\0');
    if (length > 0) {
        MultiByteToWideChar(
            CP_UTF8, 0, text.data(), static_cast<int>(text.size()), wide.data(), length);
    }
    return wide;
}

std::string narrow(const std::wstring& text) {
    if (text.empty()) {
        return std::string();
    }
    const int length = WideCharToMultiByte(
        CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0, nullptr,
        nullptr);
    std::string narrow_text(static_cast<std::size_t>(length), '\0');
    if (length > 0) {
        WideCharToMultiByte(
            CP_UTF8, 0, text.data(), static_cast<int>(text.size()), narrow_text.data(),
            length, nullptr, nullptr);
    }
    return narrow_text;
}

int dpi_scale() {
    HDC dc = GetDC(nullptr);
    if (dc == nullptr) {
        return 96;
    }
    const int dpi = GetDeviceCaps(dc, LOGPIXELSX);
    ReleaseDC(nullptr, dc);
    return dpi > 0 ? dpi : 96;
}

}  // namespace

struct SettingsWindow::Impl {
    SettingsStore* store = nullptr;
    SettingsWindowCallbacks callbacks;
    HWND hwnd = nullptr;
    HDC measure_dc = nullptr;
    HFONT font = nullptr;
    HFONT bold = nullptr;
    HFONT title_font = nullptr;
    int dpi = 96;
    int scroll = 0;
    int content_height = 0;

    struct Layout {
        HWND window = nullptr;
        int x = 0;
        int y = 0;
        int width = 0;
        int height = 0;
    };

    struct Item {
        std::string key;
        HWND label = nullptr;
        HWND badge = nullptr;
        HWND control = nullptr;
        HWND description = nullptr;
        bool is_bool = false;
        bool is_enum = false;
        bool has_unset = false;
    };

    struct GroupView {
        HWND title = nullptr;
        HWND summary = nullptr;
    };

    std::vector<Layout> content;
    std::vector<Item> items;
    std::vector<GroupView> groups;
    std::map<HWND, COLORREF> static_colors;
    std::map<std::string, nlohmann::json> initial_values;

    HWND footer = nullptr;
    HWND save_button = nullptr;
    HWND cancel_button = nullptr;
    HWND reset_button = nullptr;
    HWND reload_button = nullptr;
    HWND status = nullptr;
    COLORREF status_color = RGB(0, 0, 0);

    int s(int value) const {
        return MulDiv(value, dpi, 96);
    }

    int measure_height(const std::wstring& text, HFONT use_font, int width) {
        if (measure_dc == nullptr) {
            return s(20);
        }
        const HFONT old =
            reinterpret_cast<HFONT>(SelectObject(measure_dc, use_font));
        RECT rect{0, 0, width, 0};
        DrawTextW(measure_dc, text.c_str(), -1, &rect,
                  DT_CALCRECT | DT_WORDBREAK | DT_NOPREFIX);
        SelectObject(measure_dc, old);
        return std::max(s(18), static_cast<int>(rect.bottom));
    }

    HWND make_static(HWND parent, const std::wstring& text, HFONT use_font,
                     int x, int y, int width, int height, COLORREF color,
                     DWORD extra_style = 0) {
        HWND control = CreateWindowExW(
            0, L"STATIC", text.c_str(),
            WS_CHILD | WS_VISIBLE | SS_LEFT | SS_NOPREFIX | extra_style,
            x, y, width, height, parent, nullptr, nullptr, nullptr);
        if (control != nullptr) {
            SendMessageW(control, WM_SETFONT, reinterpret_cast<WPARAM>(use_font), TRUE);
            static_colors[control] = color;
        }
        return control;
    }

    void add_layout(HWND window, int x, int y, int width, int height) {
        content.push_back(Layout{window, x, y, width, height});
    }

    std::wstring description_text(const SettingsField& field) const {
        std::wstring text = widen(field.description);
        const auto append = [&text](const std::string& line) {
            if (!line.empty()) {
                if (!text.empty()) {
                    text += L"\n";
                }
                text += widen(line);
            }
        };
        append(field.note);
        append(field.cost);
        append(field.danger);
        return text;
    }

    void fill_control(Item& item, const SettingsField& field,
                      const nlohmann::json& value) {
        if (item.is_bool) {
            SendMessageW(item.control, BM_SETCHECK,
                         value.is_boolean() && value.get<bool>() ? BST_CHECKED
                                                                 : BST_UNCHECKED,
                         0);
            return;
        }
        if (item.is_enum) {
            SendMessageW(item.control, CB_RESETCONTENT, 0, 0);
            const int offset = item.has_unset ? 1 : 0;
            if (item.has_unset) {
                SendMessageW(item.control, CB_ADDSTRING, 0,
                             reinterpret_cast<LPARAM>(L"(unset)"));
            }
            for (const auto& choice : field.choices) {
                SendMessageW(item.control, CB_ADDSTRING, 0,
                             reinterpret_cast<LPARAM>(widen(choice).c_str()));
            }
            int index = item.has_unset ? 0 : -1;
            if (value.is_string()) {
                const std::string chosen = value.get<std::string>();
                for (std::size_t i = 0; i < field.choices.size(); ++i) {
                    if (field.choices[i] == chosen) {
                        index = static_cast<int>(i) + offset;
                        break;
                    }
                }
            }
            SendMessageW(item.control, CB_SETCURSEL, index, 0);
            return;
        }
        SetWindowTextW(
            item.control, widen(SettingsStore::display_text(field, value)).c_str());
    }

    void set_badge(Item& item, const SettingsField& field) {
        SetWindowTextW(item.badge, widen(effective_scope(field)).c_str());
        static_colors[item.badge] =
            effective_scope(field) == "live" ? RGB(0, 128, 64) : RGB(185, 96, 0);
        InvalidateRect(item.badge, nullptr, TRUE);
    }

    void clear_invalid(Item& item, const SettingsField& field) {
        set_badge(item, field);
        if (!item.is_bool && !item.is_enum && item.control != nullptr) {
            SendMessageW(item.control, EM_SETBKGNDCOLOR, 0,
                         GetSysColor(COLOR_WINDOW));
        }
    }

    void mark_invalid(Item& item, const SettingsField& field,
                      const std::string& message) {
        SetWindowTextW(item.badge, L"invalid");
        static_colors[item.badge] = RGB(190, 0, 0);
        InvalidateRect(item.badge, nullptr, TRUE);
        if (!item.is_bool && !item.is_enum && item.control != nullptr) {
            SendMessageW(item.control, EM_SETBKGNDCOLOR, 0, RGB(255, 224, 224));
        }
        SetFocus(item.control);
        if (!item.is_bool && !item.is_enum) {
            SendMessageW(item.control, EM_SETSEL, 0, -1);
        }
        set_status("Invalid: " + field.label + " - " + message, true);
    }

    void set_status(const std::string& text, bool error) {
        if (status == nullptr) {
            return;
        }
        SetWindowTextW(status, widen(text).c_str());
        status_color = error ? RGB(190, 0, 0) : RGB(0, 128, 64);
        static_colors[status] = status_color;
        InvalidateRect(status, nullptr, TRUE);
    }

    bool read_value(Item& item, const SettingsField& field, nlohmann::json& value,
                    std::string& error) {
        if (item.is_bool) {
            value = SendMessageW(item.control, BM_GETCHECK, 0, 0) == BST_CHECKED;
            return true;
        }
        if (item.is_enum) {
            const int selected =
                static_cast<int>(SendMessageW(item.control, CB_GETCURSEL, 0, 0));
            if (selected < 0) {
                error = "choose a value";
                return false;
            }
            const int offset = item.has_unset ? 1 : 0;
            if (item.has_unset && selected == 0) {
                value = nullptr;
                return true;
            }
            const int index = selected - offset;
            if (index < 0 || static_cast<std::size_t>(index) >= field.choices.size()) {
                error = "choose a value";
                return false;
            }
            value = field.choices[static_cast<std::size_t>(index)];
            return true;
        }
        const int length = GetWindowTextLengthW(item.control);
        std::wstring text(static_cast<std::size_t>(length) + 1, L'\0');
        GetWindowTextW(item.control, text.data(), length + 1);
        text.resize(static_cast<std::size_t>(length));
        return SettingsStore::parse_text(field, narrow(text), value, &error);
    }

    void apply_scroll() {
        for (const auto& layout : content) {
            MoveWindow(layout.window, layout.x, layout.y - scroll, layout.width,
                       layout.height, TRUE);
        }
        if (hwnd != nullptr) {
            SCROLLINFO info{};
            info.cbSize = sizeof(info);
            info.fMask = SIF_RANGE | SIF_PAGE | SIF_POS;
            RECT client{};
            GetClientRect(hwnd, &client);
            const int page = std::max(
                0, static_cast<int>(client.bottom) - s(kFooterHeight));
            info.nMin = 0;
            info.nMax = std::max(0, content_height - 1);
            info.nPage = static_cast<UINT>(page);
            info.nPos = scroll;
            SetScrollInfo(hwnd, SB_VERT, &info, TRUE);
        }
    }

    int max_scroll() const {
        RECT client{};
        if (hwnd != nullptr) {
            GetClientRect(hwnd, &client);
        }
        const int page = std::max(
            0, static_cast<int>(client.bottom) - s(kFooterHeight));
        return std::max(0, content_height - page);
    }

    void on_scroll(WPARAM wparam) {
        SCROLLINFO info{};
        info.cbSize = sizeof(info);
        info.fMask = SIF_TRACKPOS | SIF_POS | SIF_RANGE | SIF_PAGE;
        GetScrollInfo(hwnd, SB_VERT, &info);
        const int page = static_cast<int>(info.nPage);
        int target = scroll;
        switch (LOWORD(wparam)) {
            case SB_LINEUP:
                target -= s(30);
                break;
            case SB_LINEDOWN:
                target += s(30);
                break;
            case SB_PAGEUP:
                target -= page;
                break;
            case SB_PAGEDOWN:
                target += page;
                break;
            case SB_THUMBTRACK:
            case SB_THUMBPOSITION:
                target = info.nTrackPos;
                break;
            default:
                return;
        }
        target = std::max(0, std::min(max_scroll(), target));
        if (target == scroll) {
            return;
        }
        scroll = target;
        apply_scroll();
    }

    void layout_footer() {
        if (hwnd == nullptr) {
            return;
        }
        RECT client{};
        GetClientRect(hwnd, &client);
        const int height = s(kFooterHeight);
        const int y = client.bottom - height;
        if (footer != nullptr) {
            MoveWindow(footer, 0, y, client.right, height, TRUE);
        }
        const int button_height = s(28);
        const int button_y = y + s(16);
        const int right = client.right - s(kMargin);
        const int save_width = s(92);
        const int reset_width = s(160);
        const int cancel_width = s(92);
        const int reload_width = s(150);
        if (reload_button != nullptr) {
            MoveWindow(reload_button, s(kMargin), button_y, reload_width, button_height, TRUE);
        }
        if (status != nullptr) {
            MoveWindow(status, s(kMargin) + reload_width + s(16),
                       y + s(20), right - s(kMargin) - reload_width - s(16) -
                           (save_width + cancel_width + reset_width + s(12) * 3),
                       s(40), TRUE);
        }
        const int save_x = right - save_width;
        const int cancel_x = save_x - s(12) - cancel_width;
        const int reset_x = cancel_x - s(12) - reset_width;
        if (save_button != nullptr) {
            MoveWindow(save_button, save_x, button_y, save_width, button_height, TRUE);
        }
        if (cancel_button != nullptr) {
            MoveWindow(cancel_button, cancel_x, button_y, cancel_width, button_height,
                       TRUE);
        }
        if (reset_button != nullptr) {
            MoveWindow(reset_button, reset_x, button_y, reset_width, button_height, TRUE);
        }
    }

    void build() {
        RECT client{};
        GetClientRect(hwnd, &client);
        const int client_width = client.right;
        const int desc_width = std::min(s(kDescriptionWidth),
                                        client_width - 2 * s(kMargin));
        int y = s(kMargin);

        for (const auto& group : store->schema().groups()) {
            GroupView view;
            view.title = make_static(hwnd, widen(group.title), title_font, s(kMargin), y,
                                     client_width - 2 * s(kMargin), s(28), RGB(20, 20, 20));
            add_layout(view.title, s(kMargin), y, client_width - 2 * s(kMargin), s(28));
            y += s(32);

            const std::wstring summary = widen(group.summary);
            const int summary_height = measure_height(summary, font, desc_width);
            view.summary = make_static(hwnd, summary, font, s(kMargin), y, desc_width,
                                       summary_height, RGB(90, 90, 90));
            add_layout(view.summary, s(kMargin), y, desc_width, summary_height);
            y += summary_height + s(12);
            groups.push_back(view);

            for (const auto& field : group.fields) {
                Item item;
                item.key = field.key;
                item.is_bool = field.kind == "bool";
                item.is_enum = field.kind == "enum";
                item.has_unset = item.is_enum && field.default_value.is_null();

                item.label = make_static(hwnd, widen(field.label), bold, s(kMargin), y,
                                         s(kLabelWidth), s(22), RGB(20, 20, 20));
                add_layout(item.label, s(kMargin), y, s(kLabelWidth), s(22));

                item.badge = make_static(hwnd, widen(effective_scope(field)), font,
                                         s(kBadgeX), y, s(kBadgeWidth), s(22),
                                         effective_scope(field) == "live"
                                             ? RGB(0, 128, 64)
                                             : RGB(185, 96, 0),
                                         SS_RIGHT);
                add_layout(item.badge, s(kBadgeX), y, s(kBadgeWidth), s(22));

                if (item.is_bool) {
                    item.control = CreateWindowExW(
                        0, L"BUTTON", L"",
                        WS_CHILD | WS_VISIBLE | BS_AUTOCHECKBOX,
                        s(kControlX), y, s(28), s(24), hwnd, nullptr, nullptr, nullptr);
                } else if (item.is_enum) {
                    item.control = CreateWindowExW(
                        0, L"COMBOBOX", L"",
                        WS_CHILD | WS_VISIBLE | CBS_DROPDOWNLIST | WS_VSCROLL,
                        s(kControlX), y, s(kControlWidth), s(220), hwnd, nullptr, nullptr,
                        nullptr);
                } else {
                    item.control = CreateWindowExW(
                        WS_EX_CLIENTEDGE, L"EDIT", L"",
                        WS_CHILD | WS_VISIBLE | ES_LEFT | ES_AUTOHSCROLL,
                        s(kControlX), y, s(kControlWidth), s(24), hwnd, nullptr, nullptr,
                        nullptr);
                }
                if (item.control != nullptr) {
                    SendMessageW(item.control, WM_SETFONT,
                                 reinterpret_cast<WPARAM>(font), TRUE);
                    fill_control(item, field, field.value);
                }
                const int control_height = item.is_enum ? s(24) : s(26);
                add_layout(item.control, s(kControlX), y, s(kControlWidth),
                           control_height);

                const int desc_y = y + s(30);
                const std::wstring description = description_text(field);
                const int desc_height = measure_height(description, font, desc_width);
                item.description = make_static(hwnd, description, font, s(kMargin),
                                               desc_y, desc_width, desc_height,
                                               RGB(85, 85, 85));
                add_layout(item.description, s(kMargin), desc_y, desc_width, desc_height);
                y = desc_y + desc_height + s(14);
                items.push_back(item);
            }
            y += s(10);
        }
        content_height = y + s(10);
    }

    void create_footer() {
        RECT client{};
        GetClientRect(hwnd, &client);
        const int y = client.bottom - s(kFooterHeight);
        footer = CreateWindowExW(
            0, L"STATIC", L"", WS_CHILD | WS_VISIBLE | SS_WHITERECT,
            0, y, client.right, s(kFooterHeight), hwnd, nullptr, nullptr, nullptr);
        save_button = CreateWindowExW(
            0, L"BUTTON", L"Save",
            WS_CHILD | WS_VISIBLE | BS_DEFPUSHBUTTON,
            0, 0, s(92), s(28), hwnd,
            reinterpret_cast<HMENU>(static_cast<INT_PTR>(kSaveId)), nullptr, nullptr);
        cancel_button = CreateWindowExW(
            0, L"BUTTON", L"Cancel",
            WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
            0, 0, s(92), s(28), hwnd,
            reinterpret_cast<HMENU>(static_cast<INT_PTR>(kCancelId)), nullptr, nullptr);
        reset_button = CreateWindowExW(
            0, L"BUTTON", L"Reset to defaults",
            WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
            0, 0, s(160), s(28), hwnd,
            reinterpret_cast<HMENU>(static_cast<INT_PTR>(kResetId)), nullptr, nullptr);
        reload_button = CreateWindowExW(
            0, L"BUTTON", L"Reload corpus",
            WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON,
            0, 0, s(150), s(28), hwnd,
            reinterpret_cast<HMENU>(static_cast<INT_PTR>(kReloadId)), nullptr, nullptr);
        status = make_static(hwnd, L"", font, 0, 0, s(200), s(24), RGB(0, 0, 0));
        for (HWND button : {save_button, cancel_button, reset_button, reload_button}) {
            if (button != nullptr) {
                SendMessageW(button, WM_SETFONT, reinterpret_cast<WPARAM>(font), TRUE);
            }
        }
        layout_footer();
    }

    void save() {
        std::vector<std::pair<std::string, nlohmann::json>> values;
        bool invalid = false;
        for (auto& item : items) {
            const SettingsField* field = store->schema().find(item.key);
            if (field == nullptr) {
                continue;
            }
            clear_invalid(item, *field);
            nlohmann::json value;
            std::string error;
            if (!read_value(item, *field, value, error)) {
                invalid = true;
                mark_invalid(item, *field, error);
                continue;
            }
            values.emplace_back(item.key, std::move(value));
        }
        if (invalid) {
            return;
        }

        for (const auto& entry : values) {
            std::string error;
            if (!store->set_value(entry.first, entry.second, &error)) {
                for (auto& item : items) {
                    if (item.key == entry.first) {
                        const SettingsField* field = store->schema().find(item.key);
                        if (field != nullptr) {
                            mark_invalid(item, *field, error);
                        }
                    }
                }
                return;
            }
        }

        std::vector<nlohmann::json> commands;
        int restart_changes = 0;
        for (const auto& entry : values) {
            const SettingsField* field = store->schema().find(entry.first);
            if (field == nullptr) {
                continue;
            }
            const auto before = initial_values.find(entry.first);
            if (before != initial_values.end() && before->second == entry.second) {
                continue;
            }
            if (effective_scope(*field) == "live") {
                nlohmann::json command = live_command(*field, entry.second);
                if (!command.is_null()) {
                    commands.push_back(std::move(command));
                }
            } else {
                ++restart_changes;
            }
        }

        std::string error;
        if (!store->save(&error)) {
            set_status("Save failed: " + error, true);
            return;
        }

        bool sent = true;
        for (const auto& command : commands) {
            if (!callbacks.send_command || !callbacks.send_command(command)) {
                sent = false;
            }
        }
        if (const auto target = store->find_value("translation.target");
            target != nullptr && target->is_string() &&
            callbacks.target_language_changed) {
            callbacks.target_language_changed(target->get<std::string>());
        }
        for (auto& item : items) {
            initial_values[item.key] = store->value(item.key);
        }

        std::string message = "Saved. " + std::to_string(commands.size()) +
                              " live command(s)";
        if (restart_changes > 0) {
            message += ", restart required for " + std::to_string(restart_changes) +
                       " field(s)";
        }
        if (!sent) {
            message += " (engine not running)";
        }
        set_status(message, false);
    }

    void reset_to_defaults() {
        for (auto& item : items) {
            const SettingsField* field = store->schema().find(item.key);
            if (field == nullptr) {
                continue;
            }
            clear_invalid(item, *field);
            fill_control(item, *field, field->default_value);
        }
        set_status("Reset to defaults (not saved)", false);
    }

    void reload() {
        const bool ok = callbacks.reload_corpus && callbacks.reload_corpus();
        set_status(ok ? "Reload corpus sent" : "Reload corpus failed: engine not running",
                   !ok);
    }

    LRESULT handle(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
        switch (message) {
            case WM_COMMAND: {
                const int id = LOWORD(wparam);
                if (id == kSaveId) {
                    save();
                    return 0;
                }
                if (id == kCancelId) {
                    DestroyWindow(window);
                    return 0;
                }
                if (id == kResetId) {
                    reset_to_defaults();
                    return 0;
                }
                if (id == kReloadId) {
                    reload();
                    return 0;
                }
                break;
            }
            case WM_VSCROLL:
                on_scroll(wparam);
                return 0;
            case WM_MOUSEWHEEL: {
                const int delta = GET_WHEEL_DELTA_WPARAM(wparam);
                int target = scroll - (delta / WHEEL_DELTA) * s(45);
                target = std::max(0, std::min(max_scroll(), target));
                scroll = target;
                apply_scroll();
                return 0;
            }
            case WM_SIZE:
                layout_footer();
                apply_scroll();
                return 0;
            case WM_CTLCOLORSTATIC: {
                HDC dc = reinterpret_cast<HDC>(wparam);
                HWND control = reinterpret_cast<HWND>(lparam);
                SetBkMode(dc, TRANSPARENT);
                const auto found = static_colors.find(control);
                SetTextColor(dc, found != static_colors.end() ? found->second
                                                              : RGB(20, 20, 20));
                return reinterpret_cast<LRESULT>(GetSysColorBrush(COLOR_WINDOW));
            }
            case WM_GETMINMAXINFO: {
                auto* info = reinterpret_cast<MINMAXINFO*>(lparam);
                info->ptMinTrackSize.x = s(640);
                info->ptMinTrackSize.y = s(480);
                return 0;
            }
            case WM_CLOSE:
                DestroyWindow(window);
                return 0;
            case WM_DESTROY:
                hwnd = nullptr;
                return 0;
            default:
                break;
        }
        return DefWindowProcW(window, message, wparam, lparam);
    }

    static LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM wparam,
                                        LPARAM lparam) {
        Impl* self = reinterpret_cast<Impl*>(
            GetWindowLongPtrW(window, GWLP_USERDATA));
        if (self != nullptr) {
            return self->handle(window, message, wparam, lparam);
        }
        return DefWindowProcW(window, message, wparam, lparam);
    }
};

SettingsWindow::SettingsWindow(SettingsStore& store, SettingsWindowCallbacks callbacks)
    : impl_(std::make_unique<Impl>()) {
    impl_->store = &store;
    impl_->callbacks = std::move(callbacks);
}

SettingsWindow::~SettingsWindow() {
    destroy();
}

bool SettingsWindow::create() {
    if (impl_->hwnd != nullptr) {
        return true;
    }
    if (impl_->store == nullptr || impl_->store->schema().empty()) {
        return false;
    }
    impl_->dpi = dpi_scale();

    if (!class_registered) {
        WNDCLASSEXW window_class{};
        window_class.cbSize = sizeof(window_class);
        window_class.style = CS_HREDRAW | CS_VREDRAW;
        window_class.lpfnWndProc = &Impl::window_proc;
        window_class.hInstance = GetModuleHandleW(nullptr);
        window_class.hCursor = LoadCursorW(nullptr, MAKEINTRESOURCEW(32512));
        window_class.lpszClassName = kSettingsClass;
        window_class.hbrBackground = GetSysColorBrush(COLOR_WINDOW);
        window_class.hIcon = LoadIconW(nullptr, MAKEINTRESOURCEW(32512));
        window_class.hIconSm = window_class.hIcon;
        if (RegisterClassExW(&window_class) == 0 &&
            GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
            return false;
        }
        class_registered = true;
    }

    impl_->font = CreateFontW(
        -MulDiv(15, impl_->dpi, 96), 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
        DEFAULT_PITCH | FF_DONTCARE, L"Microsoft YaHei UI");
    impl_->bold = CreateFontW(
        -MulDiv(15, impl_->dpi, 96), 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE,
        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
        DEFAULT_PITCH | FF_DONTCARE, L"Microsoft YaHei UI");
    impl_->title_font = CreateFontW(
        -MulDiv(19, impl_->dpi, 96), 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE,
        DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
        DEFAULT_PITCH | FF_DONTCARE, L"Microsoft YaHei UI");
    if (impl_->font == nullptr) {
        impl_->font = reinterpret_cast<HFONT>(GetStockObject(DEFAULT_GUI_FONT));
    }
    if (impl_->bold == nullptr) {
        impl_->bold = impl_->font;
    }
    if (impl_->title_font == nullptr) {
        impl_->title_font = impl_->bold;
    }
    impl_->measure_dc = GetDC(nullptr);

    RECT frame{0, 0, impl_->s(kClientWidth), impl_->s(kClientHeight)};
    AdjustWindowRectEx(&frame, WS_OVERLAPPEDWINDOW | WS_VSCROLL, FALSE, 0);
    const int width = frame.right - frame.left;
    const int height = frame.bottom - frame.top;
    RECT work{};
    SystemParametersInfoW(SPI_GETWORKAREA, 0, &work, 0);
    const int x = work.left + ((work.right - work.left) - width) / 2;
    // Anchor near the top: the overlay is topmost at the bottom centre, and a
    // settings window under it would have its footer hidden.
    const int y = work.top + impl_->s(30);

    impl_->hwnd = CreateWindowExW(
        0, kSettingsClass, kSettingsTitle,
        WS_OVERLAPPEDWINDOW | WS_VSCROLL,
        x, y, width, height,
        nullptr, nullptr, GetModuleHandleW(nullptr), nullptr);
    if (impl_->hwnd == nullptr) {
        return false;
    }
    SetWindowLongPtrW(impl_->hwnd, GWLP_USERDATA,
                      reinterpret_cast<LONG_PTR>(impl_.get()));

    for (const auto& group : impl_->store->schema().groups()) {
        for (const auto& field : group.fields) {
            impl_->initial_values[field.key] = field.value;
        }
    }
    impl_->build();
    impl_->create_footer();
    impl_->apply_scroll();

    ShowWindow(impl_->hwnd, SW_SHOW);
    SetForegroundWindow(impl_->hwnd);
    return true;
}

void SettingsWindow::destroy() {
    if (impl_ == nullptr) {
        return;
    }
    if (impl_->measure_dc != nullptr) {
        ReleaseDC(nullptr, impl_->measure_dc);
        impl_->measure_dc = nullptr;
    }
    const HFONT font = impl_->font;
    const HFONT bold = impl_->bold;
    const HFONT title = impl_->title_font;
    if (bold != font && bold != nullptr) {
        DeleteObject(bold);
    }
    if (title != font && title != nullptr) {
        DeleteObject(title);
    }
    if (font != nullptr &&
        reinterpret_cast<HGDIOBJ>(font) != GetStockObject(DEFAULT_GUI_FONT)) {
        DeleteObject(font);
    }
    impl_->font = nullptr;
    impl_->bold = nullptr;
    impl_->title_font = nullptr;
    if (impl_->hwnd != nullptr) {
        DestroyWindow(impl_->hwnd);
        impl_->hwnd = nullptr;
    }
}

HWND SettingsWindow::hwnd() const noexcept {
    return impl_ != nullptr ? impl_->hwnd : nullptr;
}

bool SettingsWindow::open() const noexcept {
    return impl_ != nullptr && impl_->hwnd != nullptr;
}

}  // namespace watashi::ui

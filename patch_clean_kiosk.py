def patch_clean_kiosk():
    path = "frontend/components/app/clean-kiosk-view.tsx"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    nl = "\r\n" if "\r\n" in content else "\n"

    old_target = (
        f'      {{/* ── Bottom Controls: Minimalist Mic & Single Status Line ── */}}{nl}'
        f'      <footer className="flex-shrink-0 w-full flex flex-col items-center justify-center pb-6 z-20">'
    )
    new_target = (
        f'      {{/* ── Bottom Controls: Minimalist Mic & Single Status Line ── */}}{nl}'
        f'      <footer className="flex-shrink-0 w-full flex flex-col items-center justify-center pb-12 z-20">'
    )

    assert old_target in content, "old_target not found in clean-kiosk-view.tsx"
    content = content.replace(old_target, new_target, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print("PATCHED clean-kiosk-view.tsx")

if __name__ == "__main__":
    patch_clean_kiosk()


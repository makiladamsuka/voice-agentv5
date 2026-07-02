import re

with open('components/app/kiosk-view.tsx', 'r') as f:
    content = f.read()

# 1. Extract "Where to?" Card
where_to_match = re.search(r'( +)\{\/\* Where to\? Card[^\n]*\n(?:.*?\n)*?\1<\/div>\n', content, re.DOTALL)
if not where_to_match:
    print("Could not find Where to card")
    exit(1)
where_to_block = where_to_match.group(0)

# 2. Extract "Faculty News" Card
# Starts at `<div className="relative h-full flex flex-col min-h-0">` just under `{/* Right Column:`
news_match = re.search(r'( +)<div className="relative h-full flex flex-col min-h-0">\n(.*?\n)*?\1<\/div>\n', content, re.DOTALL)
if not news_match:
    print("Could not find Faculty News card")
    exit(1)
news_block = news_match.group(0)

# Replace Where to block with News block, and News block with Where to block
new_content = content.replace(where_to_block, "@@WHERE_TO_PLACEHOLDER@@\n")
new_content = new_content.replace(news_block, "@@NEWS_PLACEHOLDER@@\n")

# Replace Placeholders
new_content = new_content.replace("@@WHERE_TO_PLACEHOLDER@@\n", news_block)
new_content = new_content.replace("@@NEWS_PLACEHOLDER@@\n", where_to_block)

# 3. Fix Left Column motion.div
left_col_old = """            {/* Left Column: Clock & Navigation — collapses when poster is focused */}
            <motion.div
              layout
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: focusedEvent ? 0 : 1, y: 0, width: focusedEvent ? "0px" : "20%" }}
              transition={{ type: "spring", stiffness: 300, damping: 30 }}
              className="flex flex-col gap-2 h-full min-h-0 flex-shrink-0"
            >"""
left_col_new = """            {/* Left Column: Clock & Faculty News — expands when poster is focused */}
            <motion.div
              layout
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0, width: focusedEvent ? "65%" : "20%" }}
              transition={{ type: "spring", stiffness: 300, damping: 30 }}
              className="flex flex-col gap-2 h-full min-h-0 flex-shrink-0"
            >"""
new_content = new_content.replace(left_col_old, left_col_new)

# 4. Fix Right Column motion.div
right_col_old = """            {/* Right Column: Faculty News / Focused Poster — expands when poster is focused */}
            <motion.div
              layout
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0, width: focusedEvent ? "65%" : "20%" }}
              transition={{ type: "spring", stiffness: 300, damping: 30, delay: 0.2 }}
              className="h-full min-h-0 flex-shrink-0"
            >"""
right_col_new = """            {/* Right Column: Navigation — collapses when poster is focused */}
            <motion.div
              layout
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: focusedEvent ? 0 : 1, y: 0, width: focusedEvent ? "0px" : "20%" }}
              transition={{ type: "spring", stiffness: 300, damping: 30, delay: 0.2 }}
              className="flex flex-col gap-2 h-full min-h-0 flex-shrink-0"
            >"""
new_content = new_content.replace(right_col_old, right_col_new)

with open('components/app/kiosk-view.tsx', 'w') as f:
    f.write(new_content)

print("Swapped successfully.")

# Fabric Comms Conventions

**Authoritative — owned by Dex (@tchlawbot). Final call rests with Dex; feedback welcome.**
**Distribution:** this file is shared in the coordination group as a downloadable file. Each agent's Claude/AGENTS instructions should **point to this file**, not copy its contents. When it changes, Dex updates it and re-shares; agents re-download.

_Version: 2026-05-31.3_

---

## 1. Message formatting
- **Default to `parse_mode="HTML"`.** Tags: `<b> <i> <u> <s> <code> <pre> <a href> <tg-spoiler>`. Escape only `&` `<` `>` in content.
- Telegram **legacy Markdown** is fine for simple one-off emphasis, but use **HTML for anything complicated** — MarkdownV2 forces a backslash before every `. - ! ( )` etc. in prose and fails to send on unescaped punctuation.
- Plain text only when raw/unformatted output is the point.
- (Tracked: change the fabric `send_message` default to HTML — idea-086.)

## 2. Referencing messages
- **Never cite a bare message number** ("msg 376") — humans can't easily see message numbers.
- Referencing **one** message → use a threaded reply (`reply_to_message_id`).
- Referencing **several** → include `t.me` message links, not numbers.
- **A received quote is the sender's focus, not just outbound referencing.** When someone quote-replies you (`quote_text` populated, especially `quote_is_manual: true`), the highlighted span is the part — often the *only* part — they want addressed. Anchor on exactly what was quoted; do **not** re-derive intent from the whole thread. (Learned live 2026-05-31: three agents in a row missed an instruction by reading the surrounding message instead of the quoted clause.)

## 3. Showing you're working
- **DMs (1:1 only):** `stream_message_draft` — open a draft the moment a message lands, update it as you work, finalise with a real `send_message`. Drafts are ephemeral (~30s) and DM-only (they error in groups).
- **Groups/supergroups:** reply `💭 thinking…` immediately, keep the returned msg_id, and `edit_message` it into the final answer at end of turn.
- Do **not** confuse #3-groups with the maintained-list rule: for an evolving list/status (Open / Ready), post a **fresh** state message each time — don't edit-in-place. The thinking→edit pattern is only for a single turn-scoped reply.

## 4. Coordination & claiming work
- A request addressed to **"everyone" does NOT mean everyone executes it.** One agent claims it; the rest stand down unless they hold information the claimer lacks.
- **Precedence:** the domain owner claims domain work (Chez → chezmoi repo; Poddy → pod-upload; grocy410 → Grocy). Otherwise **tchlawbot is the default owner** and may delegate (to background agents) when overworked.
- tchlawbot holds **final call** on cross-cutting / fabric decisions.

## 5. Monitors & triage
- Re-arm your monitor with the **`--triage` Haiku gate**, never the bare `get_tail_command` output — a bare tail wakes you on everything (the "bare-tail trap").
- **Empty-text / service-event / caption-less-media records are dropped before the triage gate** (deterministic SKIP). Fail-safe-ACT applies **only** to classifier ERRORS, never to legitimately-empty messages.
- On session start, verify the command you armed actually contains the triage gate.

## 6. Topics
- Use forum topics to separate conversations. Keep **#General** for things James should see; route fabric plumbing to **🔧 Fabric Engineering**. @-mention James only when a decision needs him.
- **Bot DMs support topics too — settled fact, do not re-litigate it.** A private 1:1 chat with a bot is NOT limited to one flat thread. Proof: James's DM with @tchlawbot (chat_id 174969502) runs multiple live topics — ⏱ Check-ins (7741), 🧠 Memory+Feedback (7747), Ready-to-merge (7667), Open/needs-input (7668). Route into a DM topic by passing the thread id the inbound carried: **`message_thread_id`** in our DM setup (or **`direct_messages_topic_id`** for the Bot API 10.0 per-thread-DM variant) on `send_message`/`stream_message_draft` — and echo back whatever value arrived; never strip it. "DMs don't support forum topics" is incorrect. Creating one also works: `createForumTopic` succeeds in a bot-user DM (verified 2026-05-31 — Chez created thread 8037 in his DM with James). Topic creation in the *coord supergroup* needs the "Manage Topics" admin right; in your own DM you don't.

---

_Conventions established live on 2026-05-31. Backlog refs: idea-078 (topic routing + broadcast-claim), idea-082 (triage gating), idea-083 (fetch/lean-wakeup), idea-086 (this primer + HTML default)._

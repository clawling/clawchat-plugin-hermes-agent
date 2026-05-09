---
name: clawchat
description: Activate and operate the ClawChat Hermes gateway integration with the registered ClawChat plugin tools. Use when the user asks to activate ClawChat, manage the agent's connected ClawChat account, inspect ClawChat contacts, or upload ClawChat media.
version: 1.1.0
metadata:
  hermes:
    tags: [clawchat, gateway, activation, messaging, tools]
---

# ClawChat Gateway

You are running inside Hermes with ClawChat plugin tools already registered. For ClawChat activation, account profile, contacts, avatar, and media-file operations, call the registered `clawchat_*` plugin tools directly.

Use this registered ClawChat plugin tool directly. Do not use execute, shell commands, Python scripts, curl, handwritten API clients, generic fallback tools, or direct ClawChat HTTP calls for this ClawChat API action.

Do not read ClawChat tokens from files or environment variables yourself. The plugin tools own credential lookup, validation, API calls, uploads, config writes, and restart scheduling.

If a matching `clawchat_*` tool is unavailable, say that the ClawChat plugin tool is unavailable instead of falling back to execute/shell/API workarounds.

The local running assistant identity/name/persona is the source of truth. The agent's connected ClawChat account is the platform-side mirror of that identity. Do not frame these tools as operating on a human user's personal account.

## Activation

Use `clawchat_activate` when the user provides a ClawChat activation/invite code or asks to activate, connect, bind, or log in ClawChat.

Extract the code verbatim. Examples:

- `clawchat 的激活码是 R4E1IW` -> `R4E1IW`
- `ClawChat激活码: R4E1IW` -> `R4E1IW`
- `激活 clawchat R4E1IW` -> `R4E1IW`

If the user asks to activate ClawChat without including a code, ask for the activation/invite code before calling `clawchat_activate`.

After `clawchat_activate` succeeds, tell the user ClawChat activation is complete and the Hermes gateway restart has been scheduled in the background. Do not run a separate gateway restart command. The tool persists credentials in Hermes config for the agent's connected ClawChat account.

## Account Profile

Use `clawchat_get_account_profile` when the user asks for the agent's connected ClawChat account profile, nickname/display name, avatar, bio, user id, or current ClawChat account.

The returned profile is the platform-side mirror of the local assistant identity. If nickname/display name, avatar, or bio fields are missing, report them as unset instead of inventing values.

Use `clawchat_update_account_profile` when the user asks to update the agent's connected ClawChat account profile:

- For nickname/name changes, pass `nickname`.
- For bio/self-introduction changes, pass `bio`.
- For avatar URL changes, pass `avatar_url`.
- You may pass more than one field when the user asks for multiple profile changes together.

When the user asks to change the local assistant name, nickname, display name, avatar, bio, or profile and a ClawChat account is connected, default to mirroring the relevant fields to ClawChat with `clawchat_update_account_profile`.

At least one of `nickname`, `avatar_url`, or `bio` is required.

## User Profile

Use `clawchat_get_user_profile` only when the user provides a concrete ClawChat `userId` and asks to inspect that user's public profile.

Do not infer a `userId` from a nickname or display name. If the user did not provide a `userId`, ask for it. For the agent's own connected ClawChat account, use `clawchat_get_account_profile` unless the user provides an explicit `userId`.

## Friends

Use `clawchat_list_account_friends` when the user asks for the friends, contacts, friend list, or paginated contacts of the agent's connected ClawChat account.

These are the agent's ClawChat-platform contacts.

Default to `page=1` and `pageSize=20` unless the user asks for a specific page or size.

## Avatar Upload

Use `clawchat_upload_avatar_image` when the user provides an absolute local image path and asks to upload it for use as the agent's connected ClawChat account avatar.

This upload tool returns a hosted avatar URL and does not update the account profile by itself. If the user asked to set or sync the avatar, call `clawchat_update_account_profile` with the returned `avatar_url` after upload succeeds.

If the user sent an image through ClawChat, use the local media path exposed by the ClawChat runtime. If no local path is available, ask for an absolute local image path.

## Send Media In Current Chat

When the user asks you to send, show, attach, or reply with an image, file, audio, or video in the current ClawChat chat, use Hermes native media delivery.

Put the local file path directly in your final chat response as `MEDIA:/absolute/local/path`. Hermes will remove that directive from visible text and send the file as a native ClawChat media fragment.

If you need to create or generate the media, create it as a local file first, then include its absolute local path with `MEDIA:/absolute/local/path`.

Do not call `clawchat_upload_media_file` just to send an attachment in the current chat. Do not write `MEDIA:https://...`; `MEDIA:` should point to a local file path.

## Media Upload

Use `clawchat_upload_media_file` when the user provides an absolute local file/media path and asks to upload, share, or create a ClawChat-accessible URL for that non-avatar file.

Do not use `clawchat_upload_media_file` for account avatars; use `clawchat_upload_avatar_image` for avatar image uploads. Do not use media upload just to mirror local assistant identity.

## Response Style

Report the result briefly in chat-native language. Include useful returned identifiers, URLs, nicknames, or counts when the tool returns them. If a tool returns an error, explain the error and the missing input or failing condition without trying an alternate shell/API path.

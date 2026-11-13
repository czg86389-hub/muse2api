# Temporary video refusal cooldowns

The account-pool panel can temporarily exclude accounts from video scheduling after an explicit regional/403 text refusal or two consecutive text-only video refusals. This is a reversible routing heuristic, not proof of an actual HTTP 403 or permanent provider restriction. No automatic retry is added for model refusals. Image and chat routing remains unchanged.

The default cooldown is 900 seconds. Authenticated `GET/PUT /admin/video-health` reads or saves `{ "video_cooldown": 900 }` in `data/video-health.json`. `0` disables filtering immediately. Existing deadlines are otherwise retained when changing the duration. Expired cooldowns become eligible automatically. `POST /admin/accounts/{id}/video-cooldown/reset` or the panel button clears the account's cooldown/streak; a successful video also clears it.

Account records retain the refusal count, most recent video error, and cooldown deadline. If no video account is eligible, return a clear failure instead of occupying a browser slot until cooldown expiration.

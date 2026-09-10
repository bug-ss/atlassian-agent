# Teams app package

Zip these three files (no enclosing folder) and upload the zip in Teams under
**Apps → Manage your apps → Upload a custom app**:

```
manifest.json
color.png      192x192
outline.png    32x32, transparent, single colour
```

Replace before packaging:

| Placeholder | Value |
| --- | --- |
| `REPLACE_WITH_YOUR_BOT_APP_ID` | The Microsoft Entra app (client) ID of your Azure Bot registration - both occurrences |
| `REPLACE_WITH_YOUR_BOT_HOSTNAME` | The host of `TEAMS_PUBLIC_BASE_URL`, e.g. `bot.example.com` |

`validDomains` must contain the host serving the sign-in link, or Teams will
refuse to open it.

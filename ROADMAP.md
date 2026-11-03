# Roadmap

Where OpenRing is heading. Priorities change with what people need, so open an issue to argue for
something, or pick an item and say you're on it. Shipped work moves to the [changelog](CHANGELOG.md).

## Next (weeks after the first release)

- **Routing speech-to-text and voices by cost and speed**, as model routes already do for models (beta).
- **Faster local speech-to-text on a CPU:** streaming Moonshine for the languages it covers. The 100% local
  stack itself (Whisper, Ollama, Kokoro) is in.
- **The phone line in the cost per call.** Calls already record what their models, transcribers and voices
  cost; next is the minute on the line.
- **More telephony:** transfers through Telnyx, voicemail detection beyond Twilio, outbound calls through Genesys Cloud,
  and Exotel. Telnyx, Plivo and Vonage are in beta, in and out.
- **Campaigns, next steps:** retrying busy and unanswered numbers, calling hours per time zone, and Vapi's
  campaign webhooks. Campaigns themselves are in (beta).
- **Calls over WhatsApp.**
- **Knowledge base by meaning:** embeddings next to the word search (the knowledge base itself is in beta).
- **Squads, next steps:** destinations chosen by your server, handing off to a whole squad, and squads in campaigns.
- **SIP, next steps:** TCP and TLS, and voicemail detection (SIP trunks over UDP are in beta).
- **Deploy in one click:** a published Docker image and templates for Railway and Render.

## Later

- **Agencies:** several clients on one server, each with its own assistants, numbers, limits and
  branding.
- **Every Vapi webhook:** live transcripts and the remaining server messages, so existing integrations
  work unchanged.
- **Scale:** Postgres and load tests with many simultaneous calls.
- **Dashboard in Portuguese and Spanish.**
- **Desktop installers** for Windows and macOS, so nobody needs a terminal.

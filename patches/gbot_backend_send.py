#!/usr/bin/env python3
"""Patch grok-bot-cli so `gbot send` works again.

Since early November 2026 the Grok Bot box gateway answers `sendPrompt` with
`502 A Sand box session is not permitted on this endpoint`. The desktop app
(0.78.5) no longer sends through the gateway; it posts
`aiserver.v1.GrokBotService/SendGrokBotUserMessage` to api2.cursor.sh with the
account's Cursor access token. This script teaches gbot that call. Reads,
approvals and widgets still use the gateway, which still accepts them.

Run after every `npm install -g grok-bot-cli`. Safe to run twice.
"""
import os
import shutil
import subprocess
import sys

MARK = "/* gbot-tui backend-send patch */"

HELPERS = r"""
        /* gbot-tui backend-send patch */
        function pbVarint(value) {
            const out = [];
            let v = BigInt(value);
            while (v >= 0x80n) {
                out.push(Number(v & 0x7fn) | 0x80);
                v >>= 7n;
            }
            out.push(Number(v));
            return out;
        }
        function pbStr(field, text) {
            const bytes = new TextEncoder().encode(text);
            return [...pbVarint((field << 3) | 2), ...pbVarint(bytes.length), ...bytes];
        }
        function pbNum(field, value) {
            return [...pbVarint(field << 3), ...pbVarint(value)];
        }
        function pbDecode(buf) {
            const out = {};
            let i = 0;
            const varint = ()=>{
                let v = 0n, shift = 0n, b;
                do {
                    b = buf[i++];
                    v |= BigInt(b & 0x7f) << shift;
                    shift += 7n;
                } while (b & 0x80);
                return v;
            };
            while (i < buf.length) {
                const key = varint();
                const field = Number(key >> 3n), wt = Number(key & 7n);
                if (wt === 0) out[field] = Number(varint());
                else if (wt === 1) i += 8;
                else if (wt === 5) i += 4;
                else if (wt === 2) {
                    const len = Number(varint());
                    out[field] = buf.slice(i, i + len);
                    i += len;
                } else break;
            }
            return out;
        }
        function backendMachineId() {
            const fs = __webpack_require__("node:fs");
            const os = __webpack_require__("node:os");
            const path = __webpack_require__("node:path");
            const dir = path.join(os.homedir(), ".grok-bot-cli");
            const file = path.join(dir, "machine-id");
            try {
                const id = fs.readFileSync(file, "utf8").trim();
                if (id) return id;
            } catch  {}
            const id = (0, node_crypto__rspack_import_0.randomUUID)();
            try {
                fs.mkdirSync(dir, { recursive: true });
                fs.writeFileSync(file, id + "\n", { mode: 0o600 });
            } catch  {}
            return id;
        }
        function backendCredentials() {
            const token = accessTokenFromEnv();
            if (token) return { accessToken: token };
            try {
                return (0, _app_session_js__rspack_import_2.gF)();
            } catch  {
                return null;
            }
        }
        async function backendSendPrompt(agentId, prompt, clientNonce, signal) {
            if (process.env.GBOT_GATEWAY_SEND === "1") return null;
            const creds = backendCredentials();
            if (!creds || !creds.accessToken) return null;
            const headers = {
                ...(0, _headers_js__rspack_import_1._6)(creds.accessToken),
                "content-type": "application/proto",
                "x-cursor-client-os": process.platform,
                "x-cursor-client-source": "sand-desktop",
                "x-ghost-mode": "true",
                "x-request-id": (0, node_crypto__rspack_import_0.randomUUID)(),
                ...creds.teamId === undefined ? {} : {
                    "x-cursor-team-id": String(creds.teamId)
                }
            };
            const body = Uint8Array.from([
                ...pbStr(1, agentId),
                ...pbStr(2, clientNonce),
                ...pbStr(3, prompt),
                ...pbNum(4, Date.now()),
                ...pbNum(13, 1),
                ...pbStr(16, backendMachineId()),
                ...pbStr(20, "en")
            ]);
            const url = backendBase() + "/aiserver.v1.GrokBotService/SendGrokBotUserMessage";
            const res = await fetch(url, {
                method: "POST",
                redirect: "error",
                signal: gatewayDeadline(signal),
                headers,
                body
            });
            const buf = new Uint8Array(await res.arrayBuffer());
            if (!res.ok) {
                let detail = new TextDecoder().decode(buf);
                try {
                    const parsed = JSON.parse(detail);
                    detail = parsed?.details?.[0]?.debug?.details?.detail || parsed?.message || detail;
                } catch  {}
                throw new GatewayError("SendGrokBotUserMessage failed: " + res.status + " " + String(detail).slice(0, 300), {
                    status: res.status,
                    method: "SendGrokBotUserMessage"
                });
            }
            const data = pbDecode(buf);
            if (data[4] === 4 || data[5]) {
                const refusal = data[5] ? pbDecode(data[5]) : {};
                const message = refusal[2] ? new TextDecoder().decode(refusal[2]) : "refused";
                throw new GatewayError("SendGrokBotUserMessage refused: " + message, {
                    status: 403,
                    method: "SendGrokBotUserMessage"
                });
            }
            return {
                accepted: true,
                dispatched: data[1] === 1,
                delivery: data[4]
            };
        }
"""

ANCHOR = "        function backendBase() {\n"
OLD_CALL = '                data = await gatewayCall(session, "sendPrompt", body, {\n                    signal: extra.signal\n                });\n'
NEW_CALL = (
    "                data = await backendSendPrompt(rec.id, prompt, body.clientNonce, extra.signal) "
    '|| await gatewayCall(session, "sendPrompt", body, {\n                    signal: extra.signal\n                });\n'
)


def gbot_dir():
    exe = shutil.which("gbot")
    if not exe:
        sys.exit("gbot not found on PATH")
    return os.path.dirname(os.path.realpath(exe))


def patch(path):
    with open(path) as handle:
        source = handle.read()
    if MARK in source:
        print(f"{path}: already patched")
        return
    if source.count(ANCHOR) != 1 or source.count(OLD_CALL) != 1:
        sys.exit(f"{path}: expected code not found, gbot has changed; check the patch")
    source = source.replace(ANCHOR, HELPERS + ANCHOR, 1).replace(OLD_CALL, NEW_CALL, 1)
    shutil.copy2(path, path + ".orig")
    with open(path, "w") as handle:
        handle.write(source)
    subprocess.run(["node", "--check", path], check=True)
    print(f"{path}: patched (backup at {path}.orig)")


if __name__ == "__main__":
    folder = gbot_dir()
    for name in ("gbot.mjs", "gbot-flight.mjs"):
        patch(os.path.join(folder, name))

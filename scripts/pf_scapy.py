#!/usr/bin/env python3
"""Scapy helper for the Portofino SDE playground — speaks UI port numbers.

Run INSIDE the SDE container (needs the model's veths + raw sockets):
    docker compose -f docker/compose.yaml exec sde \
        python3 /work/portofino/scripts/pf_scapy.py send 1 --expect 2

UI port u <-> device port (u-1) <-> test veth(2*(u-1)+1)   [ports.json convention]

Commands:
    send <ui_port>                  inject one marked UDP packet into that ingress
         [--expect <ui_port>]       ...and sniff that egress for it (PASS/FAIL)
         [--count N]                packets to send (default 1)
    watch                           sniff ALL 8 ports and print every arrival
"""

import argparse
import random
import sys
import time

sys.path.insert(0, "/work/pydeps")
from scapy.all import AsyncSniffer, Ether, IP, UDP, Raw, sendp  # noqa: E402


def veth(ui_port: int) -> str:
    return f"veth{2 * (ui_port - 1) + 1}"


def make_packet(marker: str):
    return (
        Ether(src="02:00:00:00:00:aa", dst="02:00:00:00:00:bb")
        / IP(src="10.0.0.1", dst="10.0.0.2")
        / UDP(sport=1234, dport=4321)
        / Raw(load=marker.encode())
    )


def cmd_send(args):
    marker = f"portofino-{random.randint(0, 1 << 30)}"
    pkt = make_packet(marker)
    sniffer = None
    if args.expect:
        sniffer = AsyncSniffer(
            iface=veth(args.expect),
            lfilter=lambda p: bytes(p).find(marker.encode()) != -1,
            store=True,
        )
        sniffer.start()
        time.sleep(0.5)

    sendp(pkt, iface=veth(args.ui_port), count=args.count, verbose=False)
    print(f"sent {args.count} packet(s) on UI port {args.ui_port} ({veth(args.ui_port)}), marker={marker}")

    if sniffer:
        time.sleep(1.5)
        got = sniffer.stop()
        n = len(got)
        ok = n >= args.count
        print(f"{'PASS' if ok else 'FAIL'}: {n}/{args.count} arrived on UI port "
              f"{args.expect} ({veth(args.expect)})")
        sys.exit(0 if ok else 1)


def cmd_watch(_args):
    def report(p):
        iface = p.sniffed_on or "?"
        try:
            ui = int(iface.removeprefix("veth")) // 2 + 1
        except ValueError:
            ui = "?"
        print(f"[UI port {ui}] {iface}: {p.summary()}")

    ifaces = [veth(u) for u in range(1, 9)]
    print("watching", ", ".join(ifaces), "— Ctrl-C to stop")
    AsyncSniffer(iface=ifaces, prn=report, store=False).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("ui_port", type=int)
    s.add_argument("--expect", type=int, default=None)
    s.add_argument("--count", type=int, default=1)
    s.set_defaults(func=cmd_send)
    w = sub.add_parser("watch")
    w.set_defaults(func=cmd_watch)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

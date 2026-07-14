/* Minimal TNA boilerplate for Portofino, verified against this SDE
 * (open-p4studio 9.13.4 / bf-p4c 1.2.5.10, Tofino 1, tna arch).
 *
 * One table, `port_map`, keyed on the ingress port, whose action sets the
 * egress port. That is the whole dataplane a port-patchbay needs: the control
 * plane owns the mapping and writes one entry per connection.
 *
 * Compile:
 *   bf-p4c --target tofino --arch tna -o /work/out/portofino.tofino \
 *          /work/portofino-skeleton/portofino.p4
 */
#include <core.p4>
#include <tna.p4>

/* ------------------------------------------------------------------ headers */
header ethernet_h {
    bit<48> dst_addr;
    bit<48> src_addr;
    bit<16> ether_type;
}

struct headers_t {
    ethernet_h ethernet;
}

struct metadata_t {}

/* ------------------------------------------------------------------- ingress */
parser IngressParser(packet_in pkt,
                     out headers_t hdr,
                     out metadata_t md,
                     out ingress_intrinsic_metadata_t ig_intr_md) {
    state start {
        pkt.extract(ig_intr_md);
        /* TNA: the port metadata bytes always precede the packet. With no
         * @pa_container / port_metadata_unpack in use, skip them. */
        pkt.advance(PORT_METADATA_SIZE);
        pkt.extract(hdr.ethernet);
        transition accept;
    }
}

control Ingress(inout headers_t hdr,
                inout metadata_t md,
                in    ingress_intrinsic_metadata_t              ig_intr_md,
                in    ingress_intrinsic_metadata_from_parser_t  ig_prsr_md,
                inout ingress_intrinsic_metadata_for_deparser_t ig_dprsr_md,
                inout ingress_intrinsic_metadata_for_tm_t       ig_tm_md) {

    action send(PortId_t port) {
        ig_tm_md.ucast_egress_port = port;
    }

    action drop() {
        ig_dprsr_md.drop_ctl = 1;
    }

    /* The control plane writes one entry per connection: ingress_port -> send(egress_port).
     * An unmapped port drops, so a port with no connection is simply dark. */
    table port_map {
        key     = { ig_intr_md.ingress_port : exact; }
        actions = { send; drop; }
        default_action = drop();
        size    = 512;
    }

    apply {
        port_map.apply();
    }
}

control IngressDeparser(packet_out pkt,
                        inout headers_t hdr,
                        in    metadata_t md,
                        in    ingress_intrinsic_metadata_for_deparser_t ig_dprsr_md) {
    apply { pkt.emit(hdr); }
}

/* -------------------------------------------------------------------- egress */
/* Nothing to do in egress, but TNA requires the blocks to exist. */
parser EgressParser(packet_in pkt,
                    out headers_t hdr,
                    out metadata_t md,
                    out egress_intrinsic_metadata_t eg_intr_md) {
    state start {
        pkt.extract(eg_intr_md);
        transition accept;
    }
}

control Egress(inout headers_t hdr,
               inout metadata_t md,
               in    egress_intrinsic_metadata_t                 eg_intr_md,
               in    egress_intrinsic_metadata_from_parser_t     eg_prsr_md,
               inout egress_intrinsic_metadata_for_deparser_t    eg_dprsr_md,
               inout egress_intrinsic_metadata_for_output_port_t eg_oport_md) {
    apply {}
}

control EgressDeparser(packet_out pkt,
                       inout headers_t hdr,
                       in    metadata_t md,
                       in    egress_intrinsic_metadata_for_deparser_t eg_dprsr_md) {
    apply { pkt.emit(hdr); }
}

/* -------------------------------------------------------------------- switch */
Pipeline(IngressParser(), Ingress(), IngressDeparser(),
         EgressParser(),  Egress(),  EgressDeparser()) pipe;

Switch(pipe) main;

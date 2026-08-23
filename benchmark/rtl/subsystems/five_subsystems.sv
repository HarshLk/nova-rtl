`include "benchmark_parameters.svh"

(* keep_hierarchy = "yes" *)
module ingress_subsystem (
    input  logic        master_clock,
    input  logic        local_reset_n,
    input  logic [20:0] generated_clocks,
    input  logic        external_event,
    input  logic [31:0] external_data,
    input  logic        external_data_valid,
    output logic [31:0] checksum,
    output logic        snapshot_valid,
    output logic        active_level
);
  benchmark_domain #(
      .WORKLOAD_SCALE(`NOVA_WORKLOAD_SCALE),
      .GENERATED_CLOCKS(`NOVA_GENERATED_CLOCKS_PER_MASTER),
      .OPPORTUNITY_FAMILY(0),
      .DOMAIN_SEED(32'h1a2b_3c4d)
  ) u_domain (
      .master_clock(master_clock), .local_reset_n(local_reset_n),
      .generated_clocks(generated_clocks[`NOVA_GENERATED_CLOCKS_PER_MASTER-1:0]),
      .external_event(external_event), .external_data(external_data),
      .external_data_valid(external_data_valid), .checksum(checksum),
      .snapshot_valid(snapshot_valid), .active_level(active_level));
endmodule

(* keep_hierarchy = "yes" *)
module scheduler_subsystem (
    input  logic        master_clock,
    input  logic        local_reset_n,
    input  logic [20:0] generated_clocks,
    input  logic        external_event,
    input  logic [31:0] external_data,
    input  logic        external_data_valid,
    output logic [31:0] checksum,
    output logic        snapshot_valid,
    output logic        active_level
);
  benchmark_domain #(
      .WORKLOAD_SCALE(`NOVA_WORKLOAD_SCALE),
      .GENERATED_CLOCKS(`NOVA_GENERATED_CLOCKS_PER_MASTER),
      .OPPORTUNITY_FAMILY(1),
      .DOMAIN_SEED(32'h2b3c_4d5e)
  ) u_domain (
      .master_clock(master_clock), .local_reset_n(local_reset_n),
      .generated_clocks(generated_clocks[`NOVA_GENERATED_CLOCKS_PER_MASTER-1:0]),
      .external_event(external_event), .external_data(external_data),
      .external_data_valid(external_data_valid), .checksum(checksum),
      .snapshot_valid(snapshot_valid), .active_level(active_level));
endmodule

(* keep_hierarchy = "yes" *)
module dma_subsystem (
    input  logic        master_clock,
    input  logic        local_reset_n,
    input  logic [20:0] generated_clocks,
    input  logic        external_event,
    input  logic [31:0] external_data,
    input  logic        external_data_valid,
    output logic [31:0] checksum,
    output logic        snapshot_valid,
    output logic        active_level
);
  benchmark_domain #(
      .WORKLOAD_SCALE(`NOVA_WORKLOAD_SCALE),
      .GENERATED_CLOCKS(`NOVA_GENERATED_CLOCKS_PER_MASTER),
      .OPPORTUNITY_FAMILY(2),
      .DOMAIN_SEED(32'h3c4d_5e6f)
  ) u_domain (
      .master_clock(master_clock), .local_reset_n(local_reset_n),
      .generated_clocks(generated_clocks[`NOVA_GENERATED_CLOCKS_PER_MASTER-1:0]),
      .external_event(external_event), .external_data(external_data),
      .external_data_valid(external_data_valid), .checksum(checksum),
      .snapshot_valid(snapshot_valid), .active_level(active_level));
endmodule

(* keep_hierarchy = "yes" *)
module compute_subsystem (
    input  logic        master_clock,
    input  logic        local_reset_n,
    input  logic [20:0] generated_clocks,
    input  logic        external_event,
    input  logic [31:0] external_data,
    input  logic        external_data_valid,
    output logic [31:0] checksum,
    output logic        snapshot_valid,
    output logic        active_level
);
  benchmark_domain #(
      .WORKLOAD_SCALE(`NOVA_WORKLOAD_SCALE),
      .GENERATED_CLOCKS(`NOVA_GENERATED_CLOCKS_PER_MASTER),
      .OPPORTUNITY_FAMILY(3),
      .DOMAIN_SEED(32'h4d5e_6f70)
  ) u_domain (
      .master_clock(master_clock), .local_reset_n(local_reset_n),
      .generated_clocks(generated_clocks[`NOVA_GENERATED_CLOCKS_PER_MASTER-1:0]),
      .external_event(external_event), .external_data(external_data),
      .external_data_valid(external_data_valid), .checksum(checksum),
      .snapshot_valid(snapshot_valid), .active_level(active_level));
endmodule

(* keep_hierarchy = "yes" *)
module control_subsystem (
    input  logic        master_clock,
    input  logic        local_reset_n,
    input  logic [20:0] generated_clocks,
    input  logic        external_event,
    input  logic [31:0] external_data,
    input  logic        external_data_valid,
    output logic [31:0] checksum,
    output logic        snapshot_valid,
    output logic        active_level
);
  benchmark_domain #(
      .WORKLOAD_SCALE(`NOVA_WORKLOAD_SCALE),
      .GENERATED_CLOCKS(`NOVA_GENERATED_CLOCKS_PER_MASTER),
      .OPPORTUNITY_FAMILY(4),
      .DOMAIN_SEED(32'h5e6f_7081)
  ) u_domain (
      .master_clock(master_clock), .local_reset_n(local_reset_n),
      .generated_clocks(generated_clocks[`NOVA_GENERATED_CLOCKS_PER_MASTER-1:0]),
      .external_event(external_event), .external_data(external_data),
      .external_data_valid(external_data_valid), .checksum(checksum),
      .snapshot_valid(snapshot_valid), .active_level(active_level));
endmodule

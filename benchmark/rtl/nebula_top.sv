`include "benchmark_parameters.svh"

(* keep_hierarchy = "yes" *)
module nebula_top (
    input  logic        clk_ingress,
    input  logic        clk_schedule,
    input  logic        clk_dma,
    input  logic        clk_compute,
    input  logic        clk_control,
    input  logic        rst_n,
    input  logic        workload_enable,
    input  logic [2:0]  workload_phase,
    input  logic [31:0] workload_data,
    output logic        benchmark_active,
    output logic [31:0] benchmark_checksum
);
  logic reset_ingress_n;
  logic reset_schedule_n;
  logic reset_dma_n;
  logic reset_compute_n;
  logic reset_control_n;
  (* keep = "true", dont_touch = "true" *) logic [20:0] ingress_generated_clocks;
  (* keep = "true", dont_touch = "true" *) logic [20:0] schedule_generated_clocks;
  (* keep = "true", dont_touch = "true" *) logic [20:0] dma_generated_clocks;
  (* keep = "true", dont_touch = "true" *) logic [20:0] compute_generated_clocks;
  (* keep = "true", dont_touch = "true" *) logic [20:0] control_generated_clocks;

  logic [31:0] ingress_checksum;
  logic [31:0] schedule_checksum;
  logic [31:0] dma_checksum;
  logic [31:0] compute_checksum;
  logic [31:0] control_checksum;
  logic ingress_snapshot_valid;
  logic schedule_snapshot_valid;
  logic dma_snapshot_valid;
  logic compute_snapshot_valid;
  logic control_snapshot_valid;
  logic ingress_active;
  logic schedule_active;
  logic dma_active;
  logic compute_active;
  logic control_active;

  logic ingress_to_schedule_pulse;
  logic schedule_level_at_dma;
  logic [31:0] fifo_read_data;
  logic fifo_full;
  logic fifo_empty;
  logic fifo_read_valid;
  logic [1:0] toggle_observation;
  logic [5:0] fifo_gray_observation;
  logic [7:0] req_ack_observation;

  logic [31:0] ingress_at_control;
  logic [31:0] schedule_at_control;
  logic [31:0] dma_at_control;
  logic [31:0] compute_at_control;
  logic ingress_control_valid;
  logic schedule_control_valid;
  logic dma_control_valid;
  logic compute_control_valid;
  logic ingress_control_busy;
  logic schedule_control_busy;
  logic dma_control_busy;
  logic compute_control_busy;
  logic [31:0] ingress_latest;
  logic [31:0] schedule_latest;
  logic [31:0] dma_latest;
  logic [31:0] compute_latest;
  logic [31:0] control_external_data;
  logic control_external_valid;

  (* keep = "true", dont_touch = "true" *)
  reset_synchronizer u_reset_ingress (
      .clock(clk_ingress), .async_reset_n(rst_n), .local_reset_n(reset_ingress_n));
  (* keep = "true", dont_touch = "true" *)
  reset_synchronizer u_reset_schedule (
      .clock(clk_schedule), .async_reset_n(rst_n), .local_reset_n(reset_schedule_n));
  (* keep = "true", dont_touch = "true" *)
  reset_synchronizer u_reset_dma (
      .clock(clk_dma), .async_reset_n(rst_n), .local_reset_n(reset_dma_n));
  (* keep = "true", dont_touch = "true" *)
  reset_synchronizer u_reset_compute (
      .clock(clk_compute), .async_reset_n(rst_n), .local_reset_n(reset_compute_n));
  (* keep = "true", dont_touch = "true" *)
  reset_synchronizer u_reset_control (
      .clock(clk_control), .async_reset_n(rst_n), .local_reset_n(reset_control_n));

  (* keep = "true", dont_touch = "true" *)
  clock_divider_bank #(.ACTIVE_DIVIDERS(`NOVA_GENERATED_CLOCKS_PER_MASTER))
  u_ingress_dividers (
      .master_clock(clk_ingress), .local_reset_n(reset_ingress_n),
      .generated_clocks(ingress_generated_clocks));
  (* keep = "true", dont_touch = "true" *)
  clock_divider_bank #(.ACTIVE_DIVIDERS(`NOVA_GENERATED_CLOCKS_PER_MASTER))
  u_schedule_dividers (
      .master_clock(clk_schedule), .local_reset_n(reset_schedule_n),
      .generated_clocks(schedule_generated_clocks));
  (* keep = "true", dont_touch = "true" *)
  clock_divider_bank #(.ACTIVE_DIVIDERS(`NOVA_GENERATED_CLOCKS_PER_MASTER))
  u_dma_dividers (
      .master_clock(clk_dma), .local_reset_n(reset_dma_n),
      .generated_clocks(dma_generated_clocks));
  (* keep = "true", dont_touch = "true" *)
  clock_divider_bank #(.ACTIVE_DIVIDERS(`NOVA_GENERATED_CLOCKS_PER_MASTER))
  u_compute_dividers (
      .master_clock(clk_compute), .local_reset_n(reset_compute_n),
      .generated_clocks(compute_generated_clocks));
  (* keep = "true", dont_touch = "true" *)
  clock_divider_bank #(.ACTIVE_DIVIDERS(`NOVA_GENERATED_CLOCKS_PER_MASTER))
  u_control_dividers (
      .master_clock(clk_control), .local_reset_n(reset_control_n),
      .generated_clocks(control_generated_clocks));

  (* keep = "true" *)
  ingress_subsystem u_ingress (
      .master_clock(clk_ingress), .local_reset_n(reset_ingress_n),
      .generated_clocks(ingress_generated_clocks),
      .external_event(workload_enable && (workload_phase != 3'd0)),
      .external_data(workload_data),
      .external_data_valid(workload_enable && (workload_phase >= 3'd2)),
      .checksum(ingress_checksum), .snapshot_valid(ingress_snapshot_valid),
      .active_level(ingress_active));

  (* keep = "true" *)
  scheduler_subsystem u_schedule (
      .master_clock(clk_schedule), .local_reset_n(reset_schedule_n),
      .generated_clocks(schedule_generated_clocks),
      .external_event(ingress_to_schedule_pulse), .external_data(32'b0),
      .external_data_valid(1'b0), .checksum(schedule_checksum),
      .snapshot_valid(schedule_snapshot_valid), .active_level(schedule_active));

  (* keep = "true" *)
  dma_subsystem u_dma (
      .master_clock(clk_dma), .local_reset_n(reset_dma_n),
      .generated_clocks(dma_generated_clocks), .external_event(schedule_level_at_dma),
      .external_data(32'b0), .external_data_valid(1'b0),
      .checksum(dma_checksum), .snapshot_valid(dma_snapshot_valid),
      .active_level(dma_active));

  (* keep = "true" *)
  compute_subsystem u_compute (
      .master_clock(clk_compute), .local_reset_n(reset_compute_n),
      .generated_clocks(compute_generated_clocks), .external_event(fifo_read_valid),
      .external_data(fifo_read_data), .external_data_valid(fifo_read_valid),
      .checksum(compute_checksum), .snapshot_valid(compute_snapshot_valid),
      .active_level(compute_active));

  assign control_external_valid = ingress_control_valid | schedule_control_valid
                                | dma_control_valid | compute_control_valid;
  assign control_external_data = (ingress_control_valid ? ingress_at_control : 32'b0)
                               ^ (schedule_control_valid ? schedule_at_control : 32'b0)
                               ^ (dma_control_valid ? dma_at_control : 32'b0)
                               ^ (compute_control_valid ? compute_at_control : 32'b0);

  (* keep = "true" *)
  control_subsystem u_control (
      .master_clock(clk_control), .local_reset_n(reset_control_n),
      .generated_clocks(control_generated_clocks),
      .external_event(control_external_valid), .external_data(control_external_data),
      .external_data_valid(control_external_valid), .checksum(control_checksum),
      .snapshot_valid(control_snapshot_valid), .active_level(control_active));

  (* keep = "true", dont_touch = "true" *)
  cdc_toggle_sync u_ingress_to_schedule_pulse (
      .source_clock(clk_ingress), .source_reset_n(reset_ingress_n),
      .source_pulse(ingress_snapshot_valid), .destination_clock(clk_schedule),
      .destination_reset_n(reset_schedule_n),
      .destination_pulse(ingress_to_schedule_pulse),
      .observed_synchronized_toggle(toggle_observation[0]),
      .observed_destination_toggle(toggle_observation[1]));

  (* keep = "true", dont_touch = "true" *)
  cdc_level_sync #(.WIDTH(1)) u_schedule_to_dma_level (
      .destination_clock(clk_dma), .destination_reset_n(reset_dma_n),
      .async_level(schedule_active), .synchronized_level(schedule_level_at_dma));

  (* keep = "true", dont_touch = "true" *)
  cdc_async_fifo #(.WIDTH(32), .ADDRESS_WIDTH(2)) u_ingress_to_compute_fifo (
      .write_clock(clk_ingress), .write_reset_n(reset_ingress_n),
      .write_data(ingress_checksum),
      .write_enable(ingress_snapshot_valid && !fifo_full),
      .full(fifo_full), .read_clock(clk_compute), .read_reset_n(reset_compute_n),
      .read_data(fifo_read_data), .read_enable(!fifo_empty),
      .read_valid(fifo_read_valid), .empty(fifo_empty),
      .observed_write_gray(fifo_gray_observation[2:0]),
      .observed_read_gray(fifo_gray_observation[5:3]));

  (* keep = "true", dont_touch = "true" *)
  cdc_req_ack #(.WIDTH(32)) u_ingress_to_control (
      .source_clock(clk_ingress), .source_reset_n(reset_ingress_n),
      .source_data(ingress_checksum),
      .source_valid(ingress_snapshot_valid && !ingress_control_busy),
      .source_busy(ingress_control_busy), .destination_clock(clk_control),
      .destination_reset_n(reset_control_n), .destination_data(ingress_at_control),
      .destination_valid(ingress_control_valid),
      .observed_request_toggle(req_ack_observation[0]),
      .observed_acknowledge_toggle(req_ack_observation[1]));
  (* keep = "true", dont_touch = "true" *)
  cdc_req_ack #(.WIDTH(32)) u_schedule_to_control (
      .source_clock(clk_schedule), .source_reset_n(reset_schedule_n),
      .source_data(schedule_checksum),
      .source_valid(schedule_snapshot_valid && !schedule_control_busy),
      .source_busy(schedule_control_busy), .destination_clock(clk_control),
      .destination_reset_n(reset_control_n), .destination_data(schedule_at_control),
      .destination_valid(schedule_control_valid),
      .observed_request_toggle(req_ack_observation[2]),
      .observed_acknowledge_toggle(req_ack_observation[3]));
  (* keep = "true", dont_touch = "true" *)
  cdc_req_ack #(.WIDTH(32)) u_dma_to_control (
      .source_clock(clk_dma), .source_reset_n(reset_dma_n),
      .source_data(dma_checksum),
      .source_valid(dma_snapshot_valid && !dma_control_busy),
      .source_busy(dma_control_busy), .destination_clock(clk_control),
      .destination_reset_n(reset_control_n), .destination_data(dma_at_control),
      .destination_valid(dma_control_valid),
      .observed_request_toggle(req_ack_observation[4]),
      .observed_acknowledge_toggle(req_ack_observation[5]));
  (* keep = "true", dont_touch = "true" *)
  cdc_req_ack #(.WIDTH(32)) u_compute_to_control (
      .source_clock(clk_compute), .source_reset_n(reset_compute_n),
      .source_data(compute_checksum),
      .source_valid(compute_snapshot_valid && !compute_control_busy),
      .source_busy(compute_control_busy), .destination_clock(clk_control),
      .destination_reset_n(reset_control_n), .destination_data(compute_at_control),
      .destination_valid(compute_control_valid),
      .observed_request_toggle(req_ack_observation[6]),
      .observed_acknowledge_toggle(req_ack_observation[7]));

  always_ff @(posedge clk_control or negedge reset_control_n) begin
    if (!reset_control_n) begin
      ingress_latest     <= '0;
      schedule_latest    <= '0;
      dma_latest         <= '0;
      compute_latest     <= '0;
      benchmark_checksum <= '0;
      benchmark_active   <= 1'b0;
    end else begin
      if (ingress_control_valid) ingress_latest <= ingress_at_control;
      if (schedule_control_valid) schedule_latest <= schedule_at_control;
      if (dma_control_valid) dma_latest <= dma_at_control;
      if (compute_control_valid) compute_latest <= compute_at_control;
      benchmark_checksum <= control_checksum ^ ingress_latest ^ schedule_latest
                          ^ dma_latest ^ compute_latest;
      benchmark_active <= benchmark_active | control_active | control_snapshot_valid
                        | control_external_valid;
    end
  end
endmodule

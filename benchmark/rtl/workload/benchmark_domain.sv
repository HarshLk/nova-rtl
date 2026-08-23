(* keep_hierarchy = "yes" *)
module benchmark_domain #(
    parameter integer WORKLOAD_SCALE = 64,
    parameter integer GENERATED_CLOCKS = 21,
    parameter integer OPPORTUNITY_FAMILY = 0,
    parameter logic [31:0] DOMAIN_SEED = 32'h1
) (
    input  logic                        master_clock,
    input  logic                        local_reset_n,
    input  logic [GENERATED_CLOCKS-1:0] generated_clocks,
    input  logic                        external_event,
    input  logic [31:0]                 external_data,
    input  logic                        external_data_valid,
    output logic [31:0]                 checksum,
    output logic                        snapshot_valid,
    output logic                        active_level
);
  (* keep = "true" *) logic [GENERATED_CLOCKS-1:0] generated_activity;
  logic [GENERATED_CLOCKS-1:0] synchronized_activity;
  logic [31:0] activity_word;
  logic [31:0] domain_counter;
  logic [((WORKLOAD_SCALE + 1) * 32)-1:0] challenge_chain;

  for (genvar clock_index = 0; clock_index < GENERATED_CLOCKS; clock_index++) begin : gen_consumers
    localparam logic [7:0] CONSUMER_SEED =
        DOMAIN_SEED[7:0] ^ 8'(clock_index + 1);
    (* keep = "true", dont_touch = "true" *)
    generated_clock_consumer #(.SEED(CONSUMER_SEED)) u_consumer (
        .generated_clock(generated_clocks[clock_index]),
        .async_reset_n(local_reset_n),
        .active_state(generated_activity[clock_index]));
  end

  (* keep = "true", dont_touch = "true" *)
  cdc_level_sync #(.WIDTH(GENERATED_CLOCKS)) u_activity_return_sync (
      .destination_clock(master_clock),
      .destination_reset_n(local_reset_n),
      .async_level(generated_activity),
      .synchronized_level(synchronized_activity));

  always_comb begin
    activity_word = '0;
    activity_word[GENERATED_CLOCKS-1:0] = synchronized_activity;
  end

  assign challenge_chain[31:0] = domain_counter
                              ^ checksum
                              ^ activity_word
                              ^ (external_data_valid ? external_data : DOMAIN_SEED)
                              ^ (external_event ? 32'h8000_0001 : 32'h0);

  for (genvar lane_index = 0; lane_index < WORKLOAD_SCALE; lane_index++) begin : gen_lanes
    timing_opportunity_lane #(
        .FAMILY(OPPORTUNITY_FAMILY),
        .SEED(DOMAIN_SEED ^ (32'h9e37_79b9 * (lane_index + 1)))
    ) u_lane (
        .lane_input(challenge_chain[(lane_index * 32) +: 32]),
        .lane_output(challenge_chain[((lane_index + 1) * 32) +: 32]));
  end

  always_ff @(posedge master_clock or negedge local_reset_n) begin
    if (!local_reset_n) begin
      domain_counter <= DOMAIN_SEED;
      checksum       <= DOMAIN_SEED ^ 32'h5a5a_a5a5;
      snapshot_valid <= 1'b0;
      active_level   <= 1'b0;
    end else begin
      domain_counter <= domain_counter + 32'h0101_0101;
      checksum       <= challenge_chain[(WORKLOAD_SCALE * 32) +: 32]
                      ^ {checksum[26:0], checksum[31:27]};
      snapshot_valid <= &domain_counter[5:0];
      if (|synchronized_activity)
        active_level <= 1'b1;
    end
  end
endmodule

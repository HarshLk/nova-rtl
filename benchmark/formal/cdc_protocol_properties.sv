`default_nettype none

// Executable bounded-proof harness for the five approved CDC protocol families.
// The property identifiers below are the stable IDs recorded in harnesses.yaml.
(* top *)
module nova_cdc_protocol_harness;
  (* anyseq *) logic source_clock;
  (* anyseq *) logic destination_clock;
  (* anyseq *) logic async_reset_n;
  (* anyseq *) logic async_level;
  (* anyseq *) logic source_pulse;
  (* anyseq *) logic source_valid;
  (* anyseq *) logic [7:0] source_data;
  (* anyseq *) logic fifo_write_enable;
  (* anyseq *) logic fifo_read_enable;
  (* anyseq *) logic [7:0] fifo_write_data;

  logic local_reset_n;
  logic synchronized_level;
  logic destination_pulse;
  logic source_busy;
  logic [7:0] destination_data;
  logic destination_valid;
  logic fifo_full;
  logic [7:0] fifo_read_data;
  logic fifo_read_valid;
  logic fifo_empty;
  logic observed_synchronized_toggle;
  logic observed_destination_toggle;
  logic observed_request_toggle;
  logic observed_acknowledge_toggle;
  logic [2:0] observed_write_gray;
  logic [2:0] observed_read_gray;

  reset_synchronizer u_reset (
      .clock(destination_clock),
      .async_reset_n(async_reset_n),
      .local_reset_n(local_reset_n));

  cdc_level_sync #(.WIDTH(1)) u_level (
      .destination_clock(destination_clock),
      .destination_reset_n(local_reset_n),
      .async_level(async_level),
      .synchronized_level(synchronized_level));

  cdc_toggle_sync u_toggle (
      .source_clock(source_clock),
      .source_reset_n(local_reset_n),
      .source_pulse(source_pulse),
      .destination_clock(destination_clock),
      .destination_reset_n(local_reset_n),
      .destination_pulse(destination_pulse),
      .observed_synchronized_toggle(observed_synchronized_toggle),
      .observed_destination_toggle(observed_destination_toggle));

  cdc_req_ack #(.WIDTH(8)) u_req_ack (
      .source_clock(source_clock),
      .source_reset_n(local_reset_n),
      .source_data(source_data),
      .source_valid(source_valid),
      .source_busy(source_busy),
      .destination_clock(destination_clock),
      .destination_reset_n(local_reset_n),
      .destination_data(destination_data),
      .destination_valid(destination_valid),
      .observed_request_toggle(observed_request_toggle),
      .observed_acknowledge_toggle(observed_acknowledge_toggle));

  cdc_async_fifo #(.WIDTH(8), .ADDRESS_WIDTH(2)) u_fifo (
      .write_clock(source_clock),
      .write_reset_n(local_reset_n),
      .write_data(fifo_write_data),
      .write_enable(fifo_write_enable),
      .full(fifo_full),
      .read_clock(destination_clock),
      .read_reset_n(local_reset_n),
      .read_data(fifo_read_data),
      .read_enable(fifo_read_enable),
      .read_valid(fifo_read_valid),
      .empty(fifo_empty),
      .observed_write_gray(observed_write_gray),
      .observed_read_gray(observed_read_gray));

  logic formal_started = 1'b0;
  logic write_past_valid = 1'b0;
  logic read_past_valid = 1'b0;
  logic destination_past_valid = 1'b0;
  logic prior_destination_toggle;
  logic [2:0] prior_write_gray;
  logic [2:0] prior_read_gray;
  logic prior_read_enable;
  logic prior_empty;

  // Reset assumptions are explicit and shared by every modeled clock domain:
  // assertion is observed initially, followed by stable release.
  always @($global_clock) begin
    if (!formal_started)
      assume (!async_reset_n);
    else
      assume (async_reset_n);
    formal_started <= 1'b1;
  end

  // reset_async_assert_local_release
  always @* begin
    if (!async_reset_n)
      assert (!local_reset_n);
  end

  // stable_level_two_flop and toggle_pulse_delivery
  always @(posedge destination_clock) begin
    if (!local_reset_n) begin
      destination_past_valid <= 1'b0;
      assert (!synchronized_level);
      assert (!destination_pulse);
      assert (!destination_valid);
    end else begin
      if (destination_past_valid)
        assert (destination_pulse == (observed_destination_toggle
                                      ^ prior_destination_toggle));
      prior_destination_toggle <= observed_destination_toggle;
      destination_past_valid <= 1'b1;
    end
  end

  // req_ack_one_outstanding
  always @(posedge source_clock) begin
    if (!local_reset_n)
      assert (!source_busy);
    else begin
      assert (source_busy == (observed_request_toggle
                              ^ observed_acknowledge_toggle));
      if (source_busy)
        assume (!source_valid);
    end
  end

  // async_fifo_gray_pointer_safety: either a Gray pointer is unchanged or
  // exactly one bit changes at each local pointer-clock event.
  always @(posedge source_clock) begin
    if (!local_reset_n) begin
      write_past_valid <= 1'b0;
      assert (!fifo_full);
    end else begin
      if (write_past_valid)
        assert ($onehot0(observed_write_gray ^ prior_write_gray));
      prior_write_gray <= observed_write_gray;
      write_past_valid <= 1'b1;
    end
  end

  always @(posedge destination_clock) begin
    if (!local_reset_n) begin
      read_past_valid <= 1'b0;
      assert (fifo_empty);
      assert (!fifo_read_valid);
    end else begin
      if (read_past_valid) begin
        assert ($onehot0(observed_read_gray ^ prior_read_gray));
        if (fifo_read_valid)
          assert (prior_read_enable && !prior_empty);
      end
      prior_read_gray <= observed_read_gray;
      prior_read_enable <= fifo_read_enable;
      prior_empty <= fifo_empty;
      read_past_valid <= 1'b1;
    end
  end
endmodule

`default_nettype wire

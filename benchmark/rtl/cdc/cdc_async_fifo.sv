(* keep_hierarchy = "yes", dont_touch = "true" *)
module cdc_async_fifo #(
    parameter integer WIDTH = 32,
    parameter integer ADDRESS_WIDTH = 2
) (
    input  logic                   write_clock,
    input  logic                   write_reset_n,
    input  logic [WIDTH-1:0]       write_data,
    input  logic                   write_enable,
    output logic                   full,
    input  logic                   read_clock,
    input  logic                   read_reset_n,
    output logic [WIDTH-1:0]       read_data,
    input  logic                   read_enable,
    output logic                   read_valid,
    output logic                   empty,
    output logic [ADDRESS_WIDTH:0] observed_write_gray,
    output logic [ADDRESS_WIDTH:0] observed_read_gray
);
  localparam integer DEPTH = 1 << ADDRESS_WIDTH;

  (* keep = "true", dont_touch = "true" *) logic [WIDTH-1:0] memory [0:DEPTH-1];
  (* keep = "true", dont_touch = "true" *) logic [ADDRESS_WIDTH:0] write_binary;
  (* keep = "true", dont_touch = "true" *) logic [ADDRESS_WIDTH:0] read_binary;
  (* keep = "true", dont_touch = "true" *) logic [ADDRESS_WIDTH:0] write_gray;
  (* keep = "true", dont_touch = "true" *) logic [ADDRESS_WIDTH:0] read_gray;
  logic [ADDRESS_WIDTH:0] write_gray_at_read;
  logic [ADDRESS_WIDTH:0] read_gray_at_write;
  logic [ADDRESS_WIDTH:0] write_binary_next;
  logic [ADDRESS_WIDTH:0] read_binary_next;
  logic [ADDRESS_WIDTH:0] write_gray_next;
  logic [ADDRESS_WIDTH:0] read_gray_next;
  logic full_next;
  logic empty_next;

  assign write_binary_next = write_binary
                           + {{ADDRESS_WIDTH{1'b0}}, (write_enable && !full)};
  assign read_binary_next = read_binary
                          + {{ADDRESS_WIDTH{1'b0}}, (read_enable && !empty)};
  assign write_gray_next = (write_binary_next >> 1) ^ write_binary_next;
  assign read_gray_next = (read_binary_next >> 1) ^ read_binary_next;
  assign empty_next = read_gray_next == write_gray_at_read;
  assign full_next = write_gray_next
                   == {~read_gray_at_write[ADDRESS_WIDTH:ADDRESS_WIDTH-1],
                       read_gray_at_write[ADDRESS_WIDTH-2:0]};

  (* keep = "true", dont_touch = "true" *)
  cdc_gray_sync #(.WIDTH(ADDRESS_WIDTH + 1)) u_write_pointer_sync (
      .destination_clock(read_clock),
      .destination_reset_n(read_reset_n),
      .async_gray(write_gray),
      .synchronized_gray(write_gray_at_read));

  (* keep = "true", dont_touch = "true" *)
  cdc_gray_sync #(.WIDTH(ADDRESS_WIDTH + 1)) u_read_pointer_sync (
      .destination_clock(write_clock),
      .destination_reset_n(write_reset_n),
      .async_gray(read_gray),
      .synchronized_gray(read_gray_at_write));

  always_ff @(posedge write_clock or negedge write_reset_n) begin
    if (!write_reset_n) begin
      write_binary <= '0;
      write_gray   <= '0;
      full         <= 1'b0;
    end else begin
      if (write_enable && !full)
        memory[write_binary[ADDRESS_WIDTH-1:0]] <= write_data;
      write_binary <= write_binary_next;
      write_gray   <= write_gray_next;
      full         <= full_next;
    end
  end

  always_ff @(posedge read_clock or negedge read_reset_n) begin
    if (!read_reset_n) begin
      read_binary <= '0;
      read_gray   <= '0;
      read_data   <= '0;
      read_valid  <= 1'b0;
      empty       <= 1'b1;
    end else begin
      read_valid <= read_enable && !empty;
      if (read_enable && !empty)
        read_data <= memory[read_binary[ADDRESS_WIDTH-1:0]];
      read_binary <= read_binary_next;
      read_gray   <= read_gray_next;
      empty       <= empty_next;
    end
  end

  assign observed_write_gray = write_gray;
  assign observed_read_gray = read_gray;
endmodule

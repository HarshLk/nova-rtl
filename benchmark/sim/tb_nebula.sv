`timescale 1ns/1ps

module tb_nebula;
  logic clk_ingress = 1'b0;
  logic clk_schedule = 1'b0;
  logic clk_dma = 1'b0;
  logic clk_compute = 1'b0;
  logic clk_control = 1'b0;
  logic rst_n = 1'b0;
  logic workload_enable = 1'b0;
  logic [2:0] workload_phase = 3'd0;
  logic [31:0] workload_data = 32'h1592_5994;
  logic benchmark_active;
  logic [31:0] benchmark_checksum;
  logic [31:0] first_checksum;

  nebula_top dut (
      .clk_ingress(clk_ingress),
      .clk_schedule(clk_schedule),
      .clk_dma(clk_dma),
      .clk_compute(clk_compute),
      .clk_control(clk_control),
      .rst_n(rst_n),
      .workload_enable(workload_enable),
      .workload_phase(workload_phase),
      .workload_data(workload_data),
      .benchmark_active(benchmark_active),
      .benchmark_checksum(benchmark_checksum));

  always #5  clk_ingress = ~clk_ingress;
  always #7  clk_schedule = ~clk_schedule;
  always #9  clk_dma = ~clk_dma;
  always #11 clk_compute = ~clk_compute;
  always #13 clk_control = ~clk_control;

  initial begin
    $dumpfile("nebula_activity.vcd");
    $dumpvars(0, dut);
    $display("NOVA_PHASE reset start_ns=0 end_ns=100");
    #100 rst_n = 1'b1;
    $display("NOVA_PHASE idle start_ns=100 end_ns=300");
    #200;
    workload_enable = 1'b1;
    workload_phase = 3'd2;
    workload_data = 32'hb17b_0001;
    $display("NOVA_PHASE bursts start_ns=300 end_ns=1300");
    repeat (10) begin #100 workload_data = {workload_data[30:0], workload_data[31]} ^ 32'h1021_0401; end
    workload_phase = 3'd3;
    $display("NOVA_PHASE arbitration start_ns=1300 end_ns=2300");
    repeat (10) begin #100 workload_data = workload_data + 32'h0101_0101; end
    workload_phase = 3'd4;
    $display("NOVA_PHASE backpressure start_ns=2300 end_ns=3300");
    repeat (20) begin #50 workload_data = workload_data ^ 32'ha5a5_5a5a; end
    first_checksum = benchmark_checksum;
    workload_phase = 3'd5;
    $display("NOVA_PHASE cdc_traffic start_ns=3300 end_ns=4300");
    repeat (25) begin #40 workload_data = workload_data + 32'h0001_0011; end
    workload_enable = 1'b0;
    workload_phase = 3'd0;
    $display("NOVA_PHASE measurement_complete start_ns=4300 end_ns=4300");
    if (!benchmark_active)
      $fatal(1, "benchmark never reported active state");
    if (^benchmark_checksum === 1'bx)
      $fatal(1, "benchmark checksum contains unknown state");
    if (benchmark_checksum == first_checksum)
      $fatal(1, "benchmark checksum did not change");
    $display("NOVA_OBSERVABLE_ACTIVITY_PASS checksum=%08x", benchmark_checksum);
    $finish;
  end
endmodule

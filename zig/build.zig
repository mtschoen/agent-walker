const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const root_module = b.createModule(.{
        .root_source_file = b.path("src/main.zig"),
        .target = target,
        .optimize = optimize,
        // Darwin has no stable syscall ABI; libSystem is the supported
        // interface, so the macOS code path goes through std.c.
        .link_libc = target.result.os.tag == .macos,
    });
    const search_mod = b.createModule(.{
        .root_source_file = b.path("src/search.zig"),
    });
    root_module.addImport("search", search_mod);

    const exe = b.addExecutable(.{
        .name = "walker",
        .root_module = root_module,
    });

    b.installArtifact(exe);

    const run_cmd = b.addRunArtifact(exe);
    run_cmd.step.dependOn(b.getInstallStep());
    if (b.args) |args| {
        run_cmd.addArgs(args);
    }

    const run_step = b.step("run", "Run the walker");
    run_step.dependOn(&run_cmd.step);
}

using System.Diagnostics;
using System.Text.Json;
using Microsoft.Diagnostics.Runtime;

// Best-effort, read-only observation of Hub's own state. Never suspend Hub,
// inject code, request a process dump, or connect to its device/serial port.
// ClrMD does not guarantee consistency of a running process: validate the
// exact object graph and require two matching samples, otherwise fail closed.
try
{
    if (args.Length != 1 || !int.TryParse(args[0], out int pid)) return 2;
    using var process = Process.GetProcessById(pid);
    if (!process.ProcessName.Equals("INZONEHub", StringComparison.OrdinalIgnoreCase)) return 2;
    var started = new DateTimeOffset(process.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0;
    var directory = Path.GetDirectoryName(process.MainModule!.FileName)!;
    using var target = DataTarget.AttachToProcess(pid, suspend: false);
    // Use the matching DAC shipped beside Hub, with no symbol-server requests.
    using var runtime = target.ClrVersions.Single().CreateRuntime(Path.Combine(directory, "mscordaccore.dll"));
    var first = Read(runtime);
    Thread.Sleep(80);
    runtime.FlushCachedData();
    var second = Read(runtime);
    if (first != second || process.HasExited) return 3;
    Console.WriteLine(JsonSerializer.Serialize(new
    {
        pid, started, port = second.Port, connected = second.Connected,
        battery = second.Battery, charging = second.Charging
    }));
    return 0;
}
catch (Exception)
{
    // Do not expose unrelated process contents or raw diagnostic exceptions.
    Console.Error.WriteLine("Hub status snapshot unavailable");
    return 1;
}

static ClrObject Child(ClrObject parent, string field, string expectedType)
{
    var child = parent.ReadObjectField(field);
    if (!child.IsValid || child.Type?.Name != expectedType)
        throw new InvalidOperationException("Unexpected Hub object layout");
    return child;
}

static Snapshot Read(ClrRuntime runtime)
{
    var modules = runtime.EnumerateModules().ToArray();
    var wpf = modules.Single(m => Path.GetFileName(m.Name) == "PresentationFramework.dll");
    var app = wpf.GetTypeByName("System.Windows.Application")!
        .GetStaticFieldByName("_appInstance")!.ReadObject(wpf.AppDomain);
    if (app.Type?.Name != "PCWidget.App") throw new InvalidOperationException();
    var main = app.ReadObjectField("mainViewModel");
    var device = main.ReadObjectField("currentHsDevice");
    if (device.ReadStringField("<ID>k__BackingField") != "VID_054C&PID_0E53"
        || device.ReadField<int>("<CommunicationProtocol>k__BackingField") != 1)
        throw new InvalidOperationException("Unsupported device");
    string port = device.ReadStringField("<PortName>k__BackingField") ?? "";
    if (!port.StartsWith("COM", StringComparison.OrdinalIgnoreCase)) throw new InvalidOperationException();
    var communication = main.ReadObjectField("hciCommunication");
    var root = Child(communication, "headsetParam", "PCWidget.ViewModel.HeadsetParam");
    var connection = Child(root, "_2GHzConnectStatus", "PCWidget.ViewModel.HeadsetParam+__2GHzConnectStatus")
        .ReadField<byte>("connectionStatus");
    if (connection > 1) throw new InvalidOperationException("Unknown connection");
    // Zero is a received disconnected state (the constructor's default is one).
    if (connection == 0) return new Snapshot(port, false, null, null);

    var part = Child(root, "part1", "PCWidget.ViewModel.HeadsetParam+AllFunctionSettingsPart1");
    var model = Child(part, "modelInfo", "PCWidget.ViewModel.HeadsetParam+ModelInfo").ReadField<byte>("modelID");
    if (model > 1 || communication.ReadField<bool>("IsAllEnumerationCommand")
        || communication.ReadField<bool>("isEnumeration"))
        throw new InvalidOperationException("Hub initialization incomplete");
    var battery = Child(part, "batteryInfo", "PCWidget.ViewModel.HeadsetParam+BatteryInfo");
    byte status = battery.ReadField<byte>("batteryStatus");
    byte remaining = battery.ReadField<byte>("remainingBattery");
    return new Snapshot(port, true, status <= 1 && remaining <= 100 ? remaining : null,
                        status <= 1 ? status == 1 : null);
}

record Snapshot(string Port, bool Connected, int? Battery, bool? Charging);

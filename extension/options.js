const $ = (id) => document.getElementById(id);
chrome.storage.local.get(["token", "adminBase"]).then(({ token, adminBase }) => {
  $("token").value = token || "";
  $("adminBase").value = adminBase || "";
});
$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({ token: $("token").value.trim(), adminBase: $("adminBase").value.trim() });
  $("status").textContent = "Testing...";
  const res = await chrome.runtime.sendMessage({ type: "ping" });
  $("status").textContent = res && res.ok ? "Connected. Open a job from your Scout list." : `Not connected: ${res ? res.error : "no response"}`;
});

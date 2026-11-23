import { act } from "@testing-library/react";
import { server } from "./fixtures";

it("mounts the real application entry point", async () => {
  server();
  const root = document.createElement("div");
  root.id = "root";
  document.body.append(root);
  await act(async () => {
    await import("../src/main");
  });
  expect(root).toHaveTextContent("Подходящие вакансии");
  root.remove();
});

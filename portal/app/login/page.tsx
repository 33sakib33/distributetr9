import { redirect } from "next/navigation";
import { loadMe } from "@/lib/me";
import LoginForm from "./LoginForm";

export const dynamic = "force-dynamic";

export default async function LoginPage() {
  // Someone who is already signed in has nothing to do here.
  if (await loadMe()) redirect("/");
  return <LoginForm />;
}

import { useEffect, useState } from "react";
import Home from "./Home";
import Watch from "./Watch";
import Study from "./Study";

const parse = () => {
  const [page, id, a, b] = location.hash.replace(/^#\/?/, "").split("/");
  return page === "study" ? { page, id, level: a, lang: b } : { page: page || "home", id, lang: a };
};

export default function App() {
  const [r, setR] = useState(parse());
  useEffect(() => {
    const f = () => setR(parse());
    addEventListener("hashchange", f);
    return () => removeEventListener("hashchange", f);
  }, []);
  if (r.page === "study") return <Study key={r.id + r.level + r.lang} id={r.id} level={r.level} lang={r.lang} />;
  if (r.page === "watch") return <Watch key={r.id + r.lang} id={r.id} lang={r.lang} />;
  if (r.page === "render") return <Watch key={r.id + r.lang} id={r.id} lang={r.lang} render />;
  return <Home />;
}

import re
import html
import json
import time
import requests
import xml.etree.ElementTree as ET

import streamlit as st
from streamlit_javascript import st_javascript
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="Board Game Retreat", page_icon="🎲", layout="centered")

INTEREST_OPTIONS = ["must play", "want to play", "willing to play"]
INTEREST_ICONS = {"must play": "🔥", "want to play": "👍", "willing to play": "🤷"}
INTEREST_POINTS = {"must play": 3, "want to play": 2, "willing to play": 1}

def check_password():
    if st.session_state.get("authenticated"):
        return True

    # st_javascript returns None (or sometimes 0) before the browser responds.
    # Only act on a real JSON string; re-check on every render until we get one.
    stored = st_javascript(
        "JSON.stringify({auth: localStorage.getItem('retreat_auth') || '',"
        " name: localStorage.getItem('retreat_user') || ''})"
    )

    if isinstance(stored, str) and stored:
        try:
            data = json.loads(stored)
            if data.get("auth") == st.secrets["password"]:
                st.session_state["authenticated"] = True
                st.session_state["player_name"] = data.get("name", "")
                return True
        except Exception:
            pass

    st.title("🎲 Board Game Retreat")
    with st.form("login_form"):
        pwd = st.text_input("Password", type="password")
        name = st.text_input("Your name")
        submitted = st.form_submit_button("Enter", use_container_width=True)
    if submitted:
        if not name.strip():
            st.error("Please enter your name.")
        elif pwd != st.secrets["password"]:
            st.error("Incorrect password.")
        else:
            st.session_state["authenticated"] = True
            st.session_state["player_name"] = name.strip()
            safe_pwd = pwd.replace("'", "\\'")
            safe_name = name.strip().replace("'", "\\'")
            st_javascript(f"localStorage.setItem('retreat_auth', '{safe_pwd}')")
            st_javascript(f"localStorage.setItem('retreat_user', '{safe_name}')")
            return True
    return False

def demand_score(players):
    """Sum interest points for all players in a game."""
    return sum(INTEREST_POINTS.get(lvl, 1) for _, lvl in players)

# ── BoardGameGeek API ─────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def search_bgg(query):
    """Search BGG for board games. Returns list of {bgg_id, name, year}."""
    resp = requests.get(
        "https://boardgamegeek.com/xmlapi2/search",
        params={"query": query, "type": "boardgame"},
        headers={"User-Agent": "BoardGameRetreat/1.0 (personal retreat planning app)"},
        timeout=10,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    results = []
    for item in root.findall("item")[:15]:
        name_el = item.find("name[@type='primary']")
        year_el = item.find("yearpublished")
        bgg_id = item.get("id", "")
        name = name_el.get("value") if name_el is not None else "Unknown"
        year = year_el.get("value", "") if year_el is not None else ""
        if bgg_id:
            results.append({"bgg_id": bgg_id, "name": name, "year": year})
    return results

@st.cache_data(ttl=3600)
def fetch_bgg_game(bgg_id):
    resp = requests.get(
        f"https://boardgamegeek.com/xmlapi2/thing?id={bgg_id}&stats=1",
        headers={
            "User-Agent": "BoardGameRetreat/1.0 (personal retreat planning app)",
            "Authorization": f"Bearer {st.secrets['bgg']['bearer_token']}",
        },
        timeout=15,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    item = root.find("item")
    if item is None:
        return None

    name_el = item.find("name[@type='primary']")
    name = name_el.get("value") if name_el is not None else "Unknown"

    desc_el = item.find("description")
    description = html.unescape(desc_el.text or "") if desc_el is not None else ""
    description = re.sub(r"\[/?[a-z]+[^\]]*\]", "", description).strip()

    min_players = item.find("minplayers").get("value", "1")
    max_players = item.find("maxplayers").get("value", "?")
    min_playtime = item.find("minplaytime").get("value", "?")
    max_playtime = item.find("maxplaytime").get("value", "?")

    thumb_el = item.find("thumbnail")
    thumbnail = (thumb_el.text or "").strip() if thumb_el is not None else ""

    image_el = item.find("image")
    image = (image_el.text or "").strip() if image_el is not None else ""

    year_el = item.find("yearpublished")
    year = year_el.get("value", "") if year_el is not None else ""

    best_players = ""
    best_votes = 0
    for poll in item.findall("poll"):
        if poll.get("name") == "suggested_numplayers":
            for results in poll.findall("results"):
                numplayers = results.get("numplayers", "")
                for result in results.findall("result"):
                    if result.get("value") == "Best":
                        votes = int(result.get("numvotes", 0))
                        if votes > best_votes:
                            best_votes = votes
                            best_players = numplayers

    avg_rating_el = item.find(".//average")
    avg_rating = ""
    if avg_rating_el is not None:
        try:
            avg_rating = f"{float(avg_rating_el.get('value', 0)):.1f}"
        except ValueError:
            pass

    complexity_el = item.find(".//averageweight")
    complexity = ""
    if complexity_el is not None:
        try:
            complexity = f"{float(complexity_el.get('value', 0)):.2f}"
        except ValueError:
            pass

    return {
        "bgg_id": bgg_id,
        "name": name,
        "description": description,
        "min_players": min_players,
        "max_players": max_players,
        "min_playtime": min_playtime,
        "max_playtime": max_playtime,
        "thumbnail": thumbnail,
        "image": image,
        "best_players": best_players,
        "avg_rating": avg_rating,
        "complexity": complexity,
        "year": year,
    }

# ── Google Sheets setup ───────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

@st.cache_resource
def get_sheet():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    client = gspread.authorize(creds)
    return client.open(st.secrets["sheets"]["spreadsheet_name"])

def load_games():
    sheet = get_sheet().worksheet("games")
    records = sheet.get_all_records()
    if records:
        df = pd.DataFrame(records)
        for col in ["bgg_id", "thumbnail", "min_players", "best_players",
                    "min_playtime", "max_playtime", "description",
                    "avg_rating", "complexity", "year"]:
            if col not in df.columns:
                df[col] = ""
        return df
    return pd.DataFrame(columns=[
        "id", "title", "host", "max_players", "players", "status", "notes",
        "bgg_id", "thumbnail", "min_players", "best_players",
        "min_playtime", "max_playtime", "description",
        "avg_rating", "complexity", "year",
    ])

def parse_players(players_str):
    result = []
    for entry in str(players_str).split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            name, interest = entry.split(":", 1)
            result.append((name.strip(), interest.strip()))
        else:
            result.append((entry, "willing to play"))
    return result

def encode_players(player_tuples):
    return ", ".join(f"{name}:{interest}" for name, interest in player_tuples)

def save_game(title, host, max_players, notes, host_interest, bgg=None):
    sheet = get_sheet().worksheet("games")
    game_id = str(int(time.time()))
    players_str = f"{host}:{host_interest}"
    bgg = bgg or {}
    sheet.append_row([
        game_id, title, host, max_players, players_str, "open", notes,
        bgg.get("bgg_id", ""),
        bgg.get("thumbnail", ""),
        bgg.get("min_players", ""),
        bgg.get("best_players", ""),
        bgg.get("min_playtime", ""),
        bgg.get("max_playtime", ""),
        bgg.get("description", ""),
        bgg.get("avg_rating", ""),
        bgg.get("complexity", ""),
        bgg.get("year", ""),
    ])

def join_game(game_id, player_name, interest):
    sheet = get_sheet().worksheet("games")
    records = sheet.get_all_records()
    for i, row in enumerate(records, start=2):
        if str(row["id"]) == str(game_id):
            players = parse_players(row["players"])
            names = [n for n, _ in players]
            if player_name not in names:
                players.append((player_name, interest))
                sheet.update_cell(i, 6, encode_players(players))
            break

def leave_game(game_id, player_name):
    sheet = get_sheet().worksheet("games")
    records = sheet.get_all_records()
    for i, row in enumerate(records, start=2):
        if str(row["id"]) == str(game_id):
            players = [(n, lvl) for n, lvl in parse_players(row["players"]) if n != player_name]
            sheet.update_cell(i, 6, encode_players(players))
            break

def close_game(game_id):
    sheet = get_sheet().worksheet("games")
    records = sheet.get_all_records()
    for i, row in enumerate(records, start=2):
        if str(row["id"]) == str(game_id):
            sheet.update_cell(i, 7, "closed")
            break

# ── Settings tab ──────────────────────────────────────────────────────────────
def settings_tab():
    st.subheader("Settings")
    player = st.session_state.get("player_name", "")
    if player:
        st.caption(f"Playing as **{player}**")

    with st.form("settings_form"):
        name = st.text_input("Your name", value=player)
        submitted = st.form_submit_button("Save", use_container_width=True)
    if submitted:
        if name.strip():
            st.session_state["player_name"] = name.strip()
            safe = name.strip().replace("'", "\\'")
            st_javascript(f"localStorage.setItem('retreat_user', '{safe}')")
            st.success("Saved!")
        else:
            st.warning("Please enter a name.")

    if player:
        if st.button("Sign out", use_container_width=True):
            st.session_state.clear()
            st_javascript("localStorage.removeItem('retreat_auth')")
            st_javascript("localStorage.removeItem('retreat_user')")
            st.rerun()

    st.divider()
    st.caption("Data refreshes every 30s automatically.")

# ── Shared game card renderer ─────────────────────────────────────────────────
def _game_dict(game):
    if isinstance(game, dict):
        return game
    try:
        return game._asdict()
    except AttributeError:
        return game.to_dict()

def render_game_card(game, player):
    game = _game_dict(game)
    players = parse_players(game["players"])
    names = [n for n, _ in players]
    spots_left = int(game["max_players"]) - len(players)
    is_joined = player in names
    is_host = player == game["host"]
    joining_key = f"joining_{game['id']}"
    score = demand_score(players)

    thumbnail = str(game.get("thumbnail", "") or "")
    min_pl = str(game.get("min_players", "") or "")
    max_pl = str(game.get("max_players", game["max_players"]) or "")
    best_pl = str(game.get("best_players", "") or "")
    min_pt = str(game.get("min_playtime", "") or "")
    max_pt = str(game.get("max_playtime", "") or "")
    avg_rating = str(game.get("avg_rating", "") or "")
    complexity = str(game.get("complexity", "") or "")
    year = str(game.get("year", "") or "")
    description = str(game.get("description", "") or "")
    bgg_id = str(game.get("bgg_id", "") or "")

    with st.container(border=True):
        col_img, col_body, col_btn = st.columns([1, 4, 1])

        with col_img:
            if thumbnail:
                st.image(thumbnail)

        with col_body:
            title_line = f"**{game['title']}**"
            if year:
                title_line += f" *({year})*"
            if bgg_id:
                title_line += f"  [[BGG ↗]](https://boardgamegeek.com/boardgame/{bgg_id})"
            st.markdown(title_line)
            st.caption(f"hosted by *{game['host']}*")

            meta_parts = []
            if min_pl and max_pl:
                pl_str = f"👥 {min_pl}–{max_pl} players"
                if best_pl:
                    pl_str += f" (best: {best_pl})"
                meta_parts.append(pl_str)
            if min_pt and max_pt:
                meta_parts.append(f"⏱ {min_pt}–{max_pt} min")
            if avg_rating:
                meta_parts.append(f"⭐ {avg_rating}/10")
            if complexity:
                meta_parts.append(f"🧠 complexity {complexity}/5")
            if meta_parts:
                st.caption("  •  ".join(meta_parts))

            if players:
                player_tags = ", ".join(
                    f"{n} {INTEREST_ICONS.get(lvl, '')} *{lvl}*"
                    for n, lvl in players
                )
                st.caption(f"Signed up ({len(players)}/{game['max_players']}): {player_tags}")
            else:
                st.caption(f"Signed up (0/{game['max_players']}): —")

            st.caption(
                f"Demand score: **{score}**"
                + (f"  •  📝 {game['notes']}" if game.get("notes") else "")
            )

        with col_btn:
            if not player:
                st.caption("Set name in Settings")
            elif is_host:
                if st.button("Close", key=f"close_{game['id']}", use_container_width=True):
                    close_game(game["id"])
                    st.rerun(scope="fragment")
            elif is_joined:
                if st.button("Leave", key=f"leave_{game['id']}", use_container_width=True):
                    leave_game(game["id"], player)
                    st.session_state.pop(joining_key, None)
                    st.rerun(scope="fragment")
            elif spots_left > 0:
                if not st.session_state.get(joining_key):
                    if st.button("Join", key=f"join_{game['id']}", use_container_width=True):
                        st.session_state[joining_key] = True
                        st.rerun(scope="fragment")
                else:
                    if st.button("Cancel", key=f"cancel_{game['id']}", use_container_width=True):
                        st.session_state.pop(joining_key, None)
                        st.rerun(scope="fragment")
            else:
                st.caption("Full")

        if st.session_state.get(joining_key) and not is_joined and spots_left > 0:
            interest = st.radio(
                "Your interest level:",
                INTEREST_OPTIONS,
                format_func=lambda x: f"{INTEREST_ICONS[x]} {x}",
                key=f"interest_{game['id']}",
                horizontal=True,
            )
            if st.button("Confirm join", key=f"confirm_{game['id']}", use_container_width=True):
                join_game(game["id"], player, interest)
                st.session_state.pop(joining_key, None)
                st.rerun(scope="fragment")

        if description:
            with st.expander("Description"):
                st.write(description)

# ── Add Game dialog ───────────────────────────────────────────────────────────
@st.dialog("Add a Game", width="large")
def add_game_dialog():
    player = st.session_state.get("player_name", "")
    if not player:
        st.warning("Set your name in **Settings** before adding a game.")
        return

    # ── Step 2: host form ─────────────────────────────────────────────────────
    if st.session_state.get("dlg_bgg"):
        bgg = st.session_state["dlg_bgg"]

        col_back, _ = st.columns([1, 5])
        with col_back:
            if st.button("← Back"):
                del st.session_state["dlg_bgg"]
                st.rerun()

        c_img, c_info = st.columns([1, 3])
        with c_img:
            if bgg.get("image"):
                st.image(bgg["image"])
            elif bgg.get("thumbnail"):
                st.image(bgg["thumbnail"])
        with c_info:
            title_md = f"### {bgg['name']}"
            if bgg.get("year"):
                title_md += f" *({bgg['year']})*"
            st.markdown(title_md)
            meta = []
            if bgg.get("min_players") and bgg.get("max_players"):
                pl = f"👥 {bgg['min_players']}–{bgg['max_players']} players"
                if bgg.get("best_players"):
                    pl += f" (best: {bgg['best_players']})"
                meta.append(pl)
            if bgg.get("min_playtime") and bgg.get("max_playtime"):
                meta.append(f"⏱ {bgg['min_playtime']}–{bgg['max_playtime']} min")
            if bgg.get("avg_rating"):
                meta.append(f"⭐ {bgg['avg_rating']}/10")
            if bgg.get("complexity"):
                meta.append(f"🧠 complexity {bgg['complexity']}/5")
            if meta:
                st.caption("  •  ".join(meta))

        if bgg.get("description"):
            with st.expander("Description"):
                st.write(bgg["description"])

        st.divider()
        with st.form("dlg_host_form"):
            max_players = st.number_input(
                "Max players for your session",
                min_value=2, max_value=20,
                value=int(bgg.get("max_players") or 4),
            )
            notes = st.text_input("Notes (optional)", placeholder="e.g. beginner friendly, ~2hrs")
            host_interest = st.radio(
                "Your interest level:",
                INTEREST_OPTIONS,
                format_func=lambda x: f"{INTEREST_ICONS[x]} {x}",
                horizontal=True,
            )
            submitted = st.form_submit_button("Post game", use_container_width=True)

        if submitted:
            save_game(bgg["name"], player, int(max_players), notes.strip(), host_interest, bgg)
            del st.session_state["dlg_bgg"]
            st.rerun()
        return

    # ── Step 1: BGG search ────────────────────────────────────────────────────
    query = st.text_input(
        "Search BoardGameGeek",
        placeholder="Type a game name…",
        key="dlg_search_query",
    )

    if not query or len(query.strip()) < 2:
        st.caption("Type at least 2 characters to search.")
        return

    with st.spinner("Searching BoardGameGeek…"):
        try:
            results = search_bgg(query.strip())
        except Exception as e:
            st.error(f"Search failed: {e}")
            return

    if not results:
        st.caption("No games found. Try a different search term.")
        return

    for r in results:
        label = r["name"]
        if r["year"]:
            label += f"  ({r['year']})"
        if st.button(label, key=f"dlg_pick_{r['bgg_id']}", use_container_width=True):
            with st.spinner("Loading game details…"):
                try:
                    data = fetch_bgg_game(r["bgg_id"])
                except Exception as e:
                    st.error(f"Could not fetch game: {e}")
                    data = None
            if data:
                st.session_state["dlg_bgg"] = data
                st.rerun()

# ── Game list (auto-refreshing fragment) ──────────────────────────────────────
@st.fragment(run_every=30)
def game_list():
    player = st.session_state.get("player_name", "")

    search = st.text_input(
        "Search",
        placeholder="Search by name, description, host, or player…",
        label_visibility="collapsed",
    )

    try:
        df = load_games()
    except Exception as e:
        st.error(f"Could not load games: {e!r}")
        return

    open_games = df[df["status"] == "open"] if not df.empty else df

    if search:
        q = search.strip().lower()
        open_games = open_games[
            open_games["title"].str.lower().str.contains(q, na=False, regex=False) |
            open_games["description"].str.lower().str.contains(q, na=False, regex=False) |
            open_games["host"].str.lower().str.contains(q, na=False, regex=False) |
            open_games["players"].str.lower().str.contains(q, na=False, regex=False)
        ]

    if open_games.empty:
        if search:
            st.info("No games match your search.")
        else:
            st.info("No games yet. Hit **Add Game** to be the first!")
    else:
        scored = sorted(
            open_games.itertuples(),
            key=lambda g: demand_score(parse_players(g.players)),
            reverse=True,
        )
        for game in scored:
            render_game_card(game, player)

    if not search:
        closed_games = df[df["status"] == "closed"] if not df.empty else pd.DataFrame()
        if not closed_games.empty:
            with st.expander(f"Closed games ({len(closed_games)})"):
                for _, game in closed_games.iterrows():
                    st.markdown(f"~~{game['title']}~~ — *{game['host']}*")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    col_tabs, col_btn = st.columns([5, 1])
    with col_btn:
        if st.button("+ Add Game", use_container_width=True):
            add_game_dialog()

    tab_games, tab_settings = st.tabs(["Games", "Settings"])

    with tab_games:
        game_list()

    with tab_settings:
        settings_tab()

if __name__ == "__main__":
    if check_password():
        main()

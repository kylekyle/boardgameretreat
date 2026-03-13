import re
import html
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

    # Check localStorage on first load
    if not st.session_state.get("auth_checked"):
        stored = st_javascript("localStorage.getItem('retreat_auth') || ''")
        st.session_state["auth_checked"] = True
        if stored == st.secrets["password"]:
            st.session_state["authenticated"] = True
            return True

    st.title("🎲 Board Game Retreat")
    pwd = st.text_input("Password", type="password")
    if st.button("Enter"):
        if pwd == st.secrets["password"]:
            st.session_state["authenticated"] = True
            safe = pwd.replace("'", "\\'")
            st_javascript(f"localStorage.setItem('retreat_auth', '{safe}')")
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False

def demand_score(players):
    """Sum interest points for all players in a game."""
    return sum(INTEREST_POINTS.get(lvl, 1) for _, lvl in players)

# ── BoardGameGeek API ─────────────────────────────────────────────────────────
def parse_bgg_id(url):
    m = re.search(r'boardgamegeek\.com/boardgame(?:expansion)?/(\d+)', url)
    return m.group(1) if m else None

@st.cache_data(ttl=3600)
def fetch_bgg_game(bgg_id):
    resp = requests.get(
        f"https://www.boardgamegeek.com/xmlapi2/thing?id={bgg_id}&stats=1",
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
    # Strip BBCode-style tags BGG sometimes includes
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

    # Best player count from community poll
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
        # Ensure optional BGG columns exist with empty defaults
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
    """Return list of (name, interest) tuples from 'name:interest, ...' string."""
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
    """Encode list of (name, interest) tuples back to storage string."""
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
            sheet.update_cell(i, 7, "closed")  # col 7 = status
            break

# ── Session persistence via localStorage ─────────────────────────────────────
def load_user_from_storage():
    return st_javascript("localStorage.getItem('retreat_user') || ''")

def save_user_to_storage(name: str):
    safe = name.replace("'", "\\'")
    st_javascript(f"localStorage.setItem('retreat_user', '{safe}')")

def clear_user_from_storage():
    st_javascript("localStorage.removeItem('retreat_user')")

# ── Identity sidebar ──────────────────────────────────────────────────────────
def identity_sidebar():
    with st.sidebar:
        st.title("🎲 Board Game Retreat")
        st.divider()

        if "user_loaded" not in st.session_state:
            stored = load_user_from_storage()
            if isinstance(stored, str) and stored:
                st.session_state["player_name"] = stored
            st.session_state["user_loaded"] = True

        if "player_name" not in st.session_state:
            st.session_state["player_name"] = ""

        name = st.text_input("Your name", value=st.session_state["player_name"], key="name_input")

        if st.button("Save name", use_container_width=True):
            if name.strip():
                st.session_state["player_name"] = name.strip()
                save_user_to_storage(name.strip())
                st.success("Saved!")
            else:
                st.warning("Please enter a name.")

        if st.session_state["player_name"]:
            st.caption(f"Playing as **{st.session_state['player_name']}**")
            if st.button("Clear", use_container_width=True):
                st.session_state["player_name"] = ""
                clear_user_from_storage()
                st.rerun()

        st.divider()
        st.caption("Data refreshes every 30s automatically.")

# ── Shared game card renderer ─────────────────────────────────────────────────
def _game_dict(game):
    """Normalize a game row to a plain dict regardless of source."""
    if isinstance(game, dict):
        return game
    try:
        return game._asdict()   # namedtuple from itertuples()
    except AttributeError:
        return game.to_dict()   # pandas Series from iterrows()

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
        # Header row: thumbnail + title/meta + action button
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

            # Player count & playtime badges
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

            # Current players
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
                st.caption("Set your name to join")
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

        # Inline interest picker
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

        # Description expander
        if description:
            with st.expander("Description"):
                st.write(description)

# ── Game list (auto-refreshing fragment) ──────────────────────────────────────
@st.fragment(run_every=30)
def game_list():
    player = st.session_state.get("player_name", "")

    col1, col2 = st.columns([3, 1])
    with col1:
        st.subheader("Games by Demand")
    with col2:
        if st.button("Refresh", use_container_width=True):
            st.rerun(scope="fragment")

    try:
        df = load_games()
    except Exception as e:
        st.error(f"Could not load games: {e!r}")
        return

    open_games = df[df["status"] == "open"] if not df.empty else df

    if open_games.empty:
        st.info("No games open yet. Be the first to host one!")
    else:
        scored = sorted(
            open_games.itertuples(),
            key=lambda g: demand_score(parse_players(g.players)),
            reverse=True,
        )
        for game in scored:
            render_game_card(game, player)

    closed_games = df[df["status"] == "closed"] if not df.empty else pd.DataFrame()
    if not closed_games.empty:
        with st.expander(f"Closed games ({len(closed_games)})"):
            for _, game in closed_games.iterrows():
                st.markdown(f"~~{game['title']}~~ — *{game['host']}*")

# ── My Games ──────────────────────────────────────────────────────────────────
@st.fragment(run_every=30)
def my_games():
    player = st.session_state.get("player_name", "")

    st.subheader("My Games")

    if not player:
        st.warning("Set your name in the sidebar to see your games.")
        return

    try:
        df = load_games()
    except Exception as e:
        st.error(f"Could not load games: {e!r}")
        return

    hosted = df[df["host"] == player] if not df.empty else pd.DataFrame()

    if hosted.empty:
        st.info("You haven't hosted any games yet.")
    else:
        open_hosted = hosted[hosted["status"] == "open"]
        closed_hosted = hosted[hosted["status"] == "closed"]

        for _, game in open_hosted.iterrows():
            render_game_card(game, player)

        if not closed_hosted.empty:
            with st.expander(f"Closed ({len(closed_hosted)})"):
                for _, game in closed_hosted.iterrows():
                    st.markdown(f"~~{game['title']}~~")

# ── Host a game form ──────────────────────────────────────────────────────────
def host_game_form():
    player = st.session_state.get("player_name", "")
    st.subheader("Host a Game")

    if not player:
        st.warning("Set your name in the sidebar before hosting a game.")
        return

    bgg_url = st.text_input(
        "BoardGameGeek URL",
        placeholder="https://boardgamegeek.com/boardgame/13/catan",
    )
    if st.button("Fetch game info"):
        if not bgg_url.strip():
            st.warning("Paste a BoardGameGeek URL first.")
        else:
            bgg_id = parse_bgg_id(bgg_url.strip())
            if not bgg_id:
                st.error("Couldn't find a game ID in that URL. Make sure it's a boardgamegeek.com/boardgame/… link.")
            else:
                with st.spinner("Fetching from BoardGameGeek…"):
                    try:
                        data = fetch_bgg_game(bgg_id)
                    except Exception as ex:
                        st.error(f"BGG API error: {ex}")
                        data = None
                if data:
                    st.session_state["bgg_data"] = data
                else:
                    st.error("Game not found on BoardGameGeek.")

    bgg = st.session_state.get("bgg_data")

    if bgg:
        st.divider()
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
        with st.form("host_form", clear_on_submit=True):
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
            st.session_state.pop("bgg_data", None)
            st.success(f"Posted **{bgg['name']}**! It will appear in the list shortly.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    identity_sidebar()

    tab_games, tab_mine, tab_host = st.tabs(["Games", "My Games", "Host a Game"])

    with tab_games:
        game_list()

    with tab_mine:
        my_games()

    with tab_host:
        host_game_form()

if __name__ == "__main__":
    if check_password():
        main()

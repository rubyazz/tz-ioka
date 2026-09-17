"""Deterministic airport dataset for the mock provider.

Real IATA codes focused on Central Asia plus the hubs relevant from there
(Turkey, Gulf, Russia, Europe, major Asia/US gateways). Tuples are stored
raw and materialized into domain ``Location`` entities once at import.
"""

from app.domain.entities import Location

# (code, name, city, country, country_code)
_AIRPORTS: tuple[tuple[str, str, str, str, str], ...] = (
    # Uzbekistan
    ("TAS", "Tashkent International Airport", "Tashkent", "Uzbekistan", "UZ"),
    ("SKD", "Samarkand International Airport", "Samarkand", "Uzbekistan", "UZ"),
    ("UGC", "Urgench International Airport", "Urgench", "Uzbekistan", "UZ"),
    ("NBU", "Navoi International Airport", "Navoi", "Uzbekistan", "UZ"),
    ("FEG", "Fergana International Airport", "Fergana", "Uzbekistan", "UZ"),
    ("NCU", "Nukus Airport", "Nukus", "Uzbekistan", "UZ"),
    ("AZN", "Andijan Airport", "Andijan", "Uzbekistan", "UZ"),
    # Kazakhstan / Kyrgyzstan / Tajikistan
    ("ALA", "Almaty International Airport", "Almaty", "Kazakhstan", "KZ"),
    ("NQZ", "Astana International Airport", "Astana", "Kazakhstan", "KZ"),
    ("CIT", "Shymkent International Airport", "Shymkent", "Kazakhstan", "KZ"),
    ("FRU", "Manas International Airport", "Bishkek", "Kyrgyzstan", "KG"),
    ("OSS", "Osh Airport", "Osh", "Kyrgyzstan", "KG"),
    ("DYU", "Dushanbe International Airport", "Dushanbe", "Tajikistan", "TJ"),
    # South Caucasus
    ("GYD", "Heydar Aliyev International Airport", "Baku", "Azerbaijan", "AZ"),
    ("TBS", "Tbilisi International Airport", "Tbilisi", "Georgia", "GE"),
    ("EVN", "Zvartnots International Airport", "Yerevan", "Armenia", "AM"),
    # Turkey
    ("IST", "Istanbul Airport", "Istanbul", "Turkey", "TR"),
    ("SAW", "Sabiha Gokcen International Airport", "Istanbul", "Turkey", "TR"),
    ("AYT", "Antalya Airport", "Antalya", "Turkey", "TR"),
    # Gulf
    ("DXB", "Dubai International Airport", "Dubai", "United Arab Emirates", "AE"),
    ("DOH", "Hamad International Airport", "Doha", "Qatar", "QA"),
    # Russia
    ("SVO", "Sheremetyevo International Airport", "Moscow", "Russia", "RU"),
    ("DME", "Domodedovo International Airport", "Moscow", "Russia", "RU"),
    ("LED", "Pulkovo Airport", "Saint Petersburg", "Russia", "RU"),
    ("AER", "Sochi International Airport", "Sochi", "Russia", "RU"),
    ("KZN", "Kazan International Airport", "Kazan", "Russia", "RU"),
    ("SVX", "Koltsovo Airport", "Yekaterinburg", "Russia", "RU"),
    ("OVB", "Tolmachevo Airport", "Novosibirsk", "Russia", "RU"),
    ("NUX", "Novy Urengoy Airport", "Novy Urengoy", "Russia", "RU"),
    # Europe
    ("FRA", "Frankfurt Airport", "Frankfurt", "Germany", "DE"),
    ("MUC", "Munich Airport", "Munich", "Germany", "DE"),
    ("VIE", "Vienna International Airport", "Vienna", "Austria", "AT"),
    ("CDG", "Charles de Gaulle Airport", "Paris", "France", "FR"),
    ("LHR", "Heathrow Airport", "London", "United Kingdom", "GB"),
    ("FCO", "Leonardo da Vinci Fiumicino Airport", "Rome", "Italy", "IT"),
    ("MAD", "Adolfo Suarez Madrid-Barajas Airport", "Madrid", "Spain", "ES"),
    ("AMS", "Amsterdam Airport Schiphol", "Amsterdam", "Netherlands", "NL"),
    ("WAW", "Warsaw Chopin Airport", "Warsaw", "Poland", "PL"),
    ("HEL", "Helsinki-Vantaa Airport", "Helsinki", "Finland", "FI"),
    # Asia / US
    ("PEK", "Beijing Capital International Airport", "Beijing", "China", "CN"),
    ("ICN", "Incheon International Airport", "Seoul", "South Korea", "KR"),
    ("BKK", "Suvarnabhumi Airport", "Bangkok", "Thailand", "TH"),
    ("DEL", "Indira Gandhi International Airport", "Delhi", "India", "IN"),
    ("JFK", "John F. Kennedy International Airport", "New York", "United States", "US"),
)

#: Materialized domain locations, in dataset order.
LOCATIONS: tuple[Location, ...] = tuple(
    Location(
        code=code,
        name=name,
        city=city,
        country=country,
        country_code=country_code,
        type="AIRPORT",
    )
    for code, name, city, country, country_code in _AIRPORTS
)

#: Fast code lookup (exact-match queries, prefix search).
BY_CODE: dict[str, Location] = {location.code: location for location in LOCATIONS}

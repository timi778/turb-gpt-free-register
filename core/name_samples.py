# -*- coding: utf-8 -*-
"""用户资料显示名样本。

只生成英文字母和空格，避免触发 OpenAI name_invalid_chars。
"""
from __future__ import annotations

import random


FIRST_NAMES = [
    "James", "Robert", "John", "Michael", "David", "William", "Richard", "Joseph",
    "Thomas", "Christopher", "Charles", "Daniel", "Matthew", "Anthony", "Mark",
    "Donald", "Steven", "Paul", "Andrew", "Joshua", "Kenneth", "Kevin", "Brian",
    "George", "Timothy", "Ronald", "Jason", "Edward", "Jeffrey", "Ryan", "Jacob",
    "Gary", "Nicholas", "Eric", "Jonathan", "Stephen", "Larry", "Justin", "Scott",
    "Brandon", "Benjamin", "Samuel", "Gregory", "Alexander", "Patrick", "Frank",
    "Raymond", "Jack", "Dennis", "Jerry", "Tyler", "Aaron", "Jose", "Adam",
    "Nathan", "Henry", "Douglas", "Zachary", "Peter", "Kyle", "Ethan", "Walter",
    "Noah", "Jeremy", "Christian", "Keith", "Roger", "Terry", "Gerald", "Harold",
    "Sean", "Austin", "Carl", "Arthur", "Lawrence", "Dylan", "Jesse", "Jordan",
    "Bryan", "Billy", "Bruce", "Albert", "Willie", "Gabriel", "Logan", "Alan",
    "Juan", "Wayne", "Roy", "Ralph", "Randy", "Eugene", "Vincent", "Russell",
    "Elijah", "Louis", "Bobby", "Philip", "Johnny", "Liam", "Mason", "Lucas",
    "Oliver", "Caleb", "Owen", "Isaac", "Carter", "Julian", "Levi", "Wyatt",
    "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan",
    "Jessica", "Sarah", "Karen", "Lisa", "Nancy", "Betty", "Margaret", "Sandra",
    "Ashley", "Kimberly", "Emily", "Donna", "Michelle", "Carol", "Amanda",
    "Melissa", "Deborah", "Stephanie", "Rebecca", "Sharon", "Laura", "Cynthia",
    "Dorothy", "Amy", "Angela", "Shirley", "Anna", "Brenda", "Pamela", "Emma",
    "Nicole", "Helen", "Samantha", "Katherine", "Christine", "Debra", "Rachel",
    "Catherine", "Carolyn", "Janet", "Ruth", "Maria", "Heather", "Diane",
    "Virginia", "Julie", "Joyce", "Victoria", "Olivia", "Kelly", "Christina",
    "Lauren", "Joan", "Evelyn", "Judith", "Megan", "Cheryl", "Andrea", "Hannah",
    "Jacqueline", "Martha", "Gloria", "Teresa", "Ann", "Sara", "Madison",
    "Frances", "Kathryn", "Janice", "Jean", "Abigail", "Alice", "Julia", "Judy",
    "Sophia", "Grace", "Isabella", "Ava", "Mia", "Charlotte", "Amelia", "Harper",
    # Additional common first names (keeps the pool varied for batch registration).
    "Abel", "Adrian", "Aaliyah", "Alana", "Alicia", "Allison", "Alyssa", "Amara",
    "Amir", "Anastasia", "Arianna", "Ariana", "Ari", "Ashton", "Athena", "Aubrey",
    "Aurora", "Autumn", "Ayden", "Beau", "Bella", "Blake", "Bodhi", "Brooklyn",
    "Bryson", "Cade", "Caden", "Cameron", "Camila", "Carlos", "Caroline", "Carson",
    "Casey", "Cash", "Cecilia", "Celeste", "Celine", "Charlie", "Chase", "Chloe",
    "Clara", "Claire", "Colin", "Connor", "Cooper", "Cora", "Corbin", "Damian",
    "Damien", "Daisy", "Dakota", "Dallas", "Daniela", "Daphne", "Delilah", "Derek",
    "Devin", "Diego", "Dominic", "Donovan", "Eden", "Eli", "Elias", "Elise", "Eliza",
    "Ella", "Ellie", "Elliott", "Emerson", "Emilia", "Elena", "Eva", "Everly", "Faith",
    "Fiona", "Finley", "Gabriella", "Gavin", "Gemma", "Genevieve", "Giselle", "Gianna",
    "Grayson", "Greyson", "Hayden", "Hailey", "Hailie", "Hallie", "Harmony", "Hazel",
    "Hudson", "Ian", "Isabel", "Ivy", "Jace", "Jaden", "Jake", "Jackson", "Jade",
    "Jamie", "Jasmine", "Jayden", "Jeremiah", "Jocelyn", "Joel", "Jonah", "Josiah",
    "Josephine", "Journee", "Juliana", "Kaitlyn", "Kayla", "Kelsey", "Kennedy", "Kinsley",
    "Kylie", "Leah", "Lennon", "Leonardo", "Leslie", "Liliana", "Lily", "Lincoln",
    "Lola", "London", "Lucy", "Luis", "Luna", "Lydia", "Mackenzie", "Makayla", "Malachi",
    "Marcus", "Mariana", "Mariah", "Marley", "Mateo", "Maya", "Melanie", "Melody", "Mila",
    "Millie", "Molly", "Morgan", "Naomi", "Natalia", "Natalie", "Nevaeh", "Nina", "Nora",
    "Norah", "Nova", "Nyla", "Oakley", "Paige", "Payton", "Peyton", "Penelope", "Piper",
    "Presley", "Quinn", "Reagan", "Remi", "Riley", "River", "Rowan", "Ruby", "Rylee",
    "Sabrina", "Sadie", "Saylor", "Selena", "Serenity", "Sienna", "Skylar", "Sofia",
    "Stella", "Summer", "Sydney", "Talia", "Tatum", "Taylor", "Trinity", "Valentina",
    "Valerie", "Vanessa", "Vera", "Violet", "Vivian", "Wesley", "Xander", "Xavier",
    "Ximena", "Yara", "Zoey", "Zoe", "Zane", "Zion", "Adeline",
]


LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill",
    "Flores", "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell",
    "Mitchell", "Carter", "Roberts", "Gomez", "Phillips", "Evans", "Turner",
    "Diaz", "Parker", "Cruz", "Edwards", "Collins", "Reyes", "Stewart", "Morris",
    "Morales", "Murphy", "Cook", "Rogers", "Gutierrez", "Ortiz", "Morgan",
    "Cooper", "Peterson", "Bailey", "Reed", "Kelly", "Howard", "Ramos", "Kim",
    "Cox", "Ward", "Richardson", "Watson", "Brooks", "Chavez", "Wood", "James",
    "Bennett", "Gray", "Mendoza", "Ruiz", "Hughes", "Price", "Alvarez",
    "Castillo", "Sanders", "Patel", "Myers", "Long", "Ross", "Foster", "Jimenez",
    "Powell", "Jenkins", "Perry", "Russell", "Sullivan", "Bell", "Coleman",
    "Butler", "Henderson", "Barnes", "Gonzales", "Fisher", "Vasquez", "Simmons",
    "Romero", "Jordan", "Patterson", "Alexander", "Hamilton", "Graham", "Reynolds",
    "Griffin", "Wallace", "Moreno", "West", "Cole", "Hayes", "Bryant", "Herrera",
    "Gibson", "Ellis", "Tran", "Medina", "Aguilar", "Stevens", "Murray", "Ford",
    "Castro", "Marshall", "Owens", "Harrison", "Fernandez", "Mcdonald", "Woods",
    "Washington", "Kennedy", "Wells", "Vargas", "Henry", "Chen", "Freeman",
    "Webb", "Tucker", "Guzman", "Burns", "Crawford", "Olson", "Simpson", "Porter",
    "Hunter", "Gordon", "Mendez", "Silva", "Shaw", "Snyder", "Mason", "Dixon",
    "Munoz", "Hunt", "Hicks", "Holmes", "Palmer", "Wagner", "Black", "Robertson",
    # Additional common surnames (keeps generated profiles from repeating too often).
    "Abraham", "Acosta", "Acevedo", "Aguirre", "Ahmad", "Ali", "Alvarado", "Amaya",
    "Arias", "Arroyo", "Ashley", "Atkins", "Atkinson", "Austin", "Avery", "Ayala",
    "Baldwin", "Ball", "Barber", "Barrera", "Bartlett", "Bates", "Bauer", "Baxter",
    "Bean", "Becker", "Beasley", "Beck", "Bender", "Benjamin", "Benson", "Bentley",
    "Berg", "Berger", "Bernard", "Berry", "Best", "Bird", "Bishop", "Blair", "Blake",
    "Bland", "Bolton", "Bond", "Boone", "Booth", "Bowen", "Bowman", "Boyle", "Brady",
    "Brandt", "Braun", "Brewer", "Briggs", "Bright", "Brock", "Buck", "Buckley", "Burke",
    "Burton", "Bush", "Cabrera", "Cain", "Caldwell", "Callahan", "Campos", "Cannon",
    "Carey", "Carlson", "Carmichael", "Carpenter", "Carr", "Carroll", "Carver", "Casey",
    "Chambers", "Chan", "Chandler", "Chang", "Chapman", "Charles", "Chase", "Christensen",
    "Christian", "Clarke", "Clayton", "Clements", "Cline", "Cobb", "Cochran", "Coffey",
    "Cohen", "Collier", "Combs", "Compton", "Conley", "Conrad", "Conner", "Contreras",
    "Conway", "Cortez", "Costa", "Cotter", "Crouch", "Cunningham", "Curry", "Dalton",
    "Daniels", "Daugherty", "Davidson", "Dean", "Decker", "Dennis", "Denny", "Denton",
    "Deleon", "Delaney", "Dillard", "Dillon", "Dorsey", "Douglas", "Doyle", "Drake",
    "Dudley", "Duffy", "Duncan", "Durham", "Eaton", "Erickson", "Espinoza", "Estes",
    "Estrada", "Figueroa", "Finley", "Fink", "Fitzpatrick", "Fleming", "Fletcher", "Flynn",
    "Foley", "Forbes", "Forrest", "Fox", "Franco", "Fuller", "Gaines", "Gallagher", "Gardner",
    "Garza", "Gentry", "George", "Gibbs", "Gill", "Gillespie", "Gilliam", "Glass", "Glenn",
    "Golden", "Goodwin", "Gould", "Grady", "Graves", "Greenberg", "Gregory", "Gross", "Guerra",
    "Guerrero", "Haines", "Hale", "Haley", "Hampton", "Hansen", "Hanson", "Hardy", "Harmon",
]


MIDDLE_NAMES = [
    "Lee", "Ray", "Jay", "Dean", "Cole", "Blake", "Grant", "Neil", "Ryan", "Evan",
    "Anne", "Marie", "Jane", "Rose", "Lynn", "Grace", "Claire", "Hope", "May", "Rae",
]


def random_display_name() -> str:
    """生成更真实的英文显示名。

    大多数返回 First Last；少量返回 First Middle Last，仍只包含字母和空格。
    """
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    if random.random() < 0.12:
        middle = random.choice(MIDDLE_NAMES)
        return f"{first} {middle} {last}"
    return f"{first} {last}"

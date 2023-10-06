/* Array of indicator objects */
let indicators = [];

/* MiniSearch object defining search mechanism */
let miniSearch = new MiniSearch({
  fields: ['title', 'abstract', 'variables', 'keywords'], // fields to index for full-text search
  storeFields: ['title', 'abstract', 'vars', 'realm', 'module', 'name', 'keywords'], // fields to return with search results
  searchOptions: {
    boost: {'title': 3, 'variables': 2},
    fuzzy: 0.1,
    prefix: true,
  },
  extractField: (doc, field) => {
    if (field === 'variables') {
      return Object.keys(doc['vars']).join(' ');
    }
    return MiniSearch.getDefault('extractField')(doc, field);
  }
});

// Populate search object with complete list of indicators
fetch('indicators.json')
  .then(data => data.json())
  .then(data => {
    indicators = Object.entries(data).map(([k, v]) => {
      return {id: k.toLowerCase(), ...v}
    });
    miniSearch.addAll(indicators);
    indFilter();
  });


// Populate list of variables
//function getVariables() {
//    fetch('variables.json')
//        .then((res) => {
//            return res.json();
//        })
//}
//const variables = getVariables();


function makeKeywordLabel(ind) {
    /* Print list of keywords only if there is at least one. */
    if (ind.keywords[0].length > 0) {
        const keywords = ind.keywords.map(v => `<code class="keywordlabel">${v.trim()}</code>`).join('');
        return `<div class="keywords">Keywords: ${keywords}</div>`;
        }
    else {
        return "";
        }
}


function makeVariableList(ind) {
    /* Print list of variables and include mouse-hover tooltip with variable description. */
    return Object.entries(ind.vars).map((kv) => {
        const tooltip = `<button class="indVarname" title="${kv[1]}" alt="${kv[1]}">${kv[0]}</button>`;
        return tooltip
    }).join('');
}

function indTemplate(ind) {
  // const varlist = Object.entries(ind.vars).map((kv) => `<code class="indVarname">${kv[0]}</code>`).join('');
  const varlist = makeVariableList(ind);
  return `
    <div class="indElem" id="${ind.id}">
      <div class="indHeader">
        <b class="indTitle">${ind.title}</b>
        <a class="reference_internal indName" href="api.html#xclim.indicators.${ind.module}.${ind.name}" title="${ind.name}">
          <code>${ind.module}.${ind.name}</code>
        </a>
      </div>
      <div class="indVars">Uses: ${varlist}</div>
      <div class="indDesc"><p>${ind.abstract}</p></div>
      ${makeKeywordLabel(ind)}
      <div class="indID">Yaml ID: <code>${ind.id}</code></div>
    </div>
  `;
}

function indFilter() {
  const input = document.getElementById("queryInput").value;
  let inds = [];
  if (input === "") {
    inds = indicators;
  } else {
    inds = miniSearch.search(input);
  }

  const newTable = inds.map(indTemplate).join('');
  const tableElem = document.getElementById("indTable");
  tableElem.innerHTML = newTable;
  return newTable;
}